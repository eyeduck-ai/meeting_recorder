from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from api.runtime import (
    get_app_job_action_service,
    get_app_job_runner,
    get_app_job_runtime_state_service,
    get_app_job_service,
    get_app_worker,
)
from database.models import (
    RecordingJob as RecordingJobModel,
)
from database.session import JobRepository, get_db
from providers import list_providers, validate_provider_name
from services.errors import ConflictError, NotFoundError, ServiceError, ValidationError
from services.job_actions import ACTIVE_RECORDING_STATUSES
from services.job_runtime_state import active_job_payload
from services.job_service import ImmediateRecordingData
from uploading.progress import get_latest_progress, get_progress

router = APIRouter(prefix="/jobs", tags=["Jobs"])


class RecordRequest(BaseModel):
    """Request to start a recording."""

    provider: str = Field(default="jitsi", description="Meeting provider", json_schema_extra={"enum": list_providers()})
    meeting_code: str = Field(..., min_length=1, description="Meeting room code or full URL")
    display_name: str = Field(default="Recorder Bot", description="Display name in meeting")
    duration_sec: int = Field(default=3600, ge=60, le=14400, description="Recording duration in seconds")
    base_url: str | None = Field(default=None, description="Custom base URL for provider")
    password: str | None = Field(default=None, description="Meeting password if required")
    lobby_wait_sec: int | None = Field(default=None, ge=0, le=1800, description="Max lobby wait time")

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return validate_provider_name(value)


class DiagnosticInfo(BaseModel):
    """Diagnostic information for failed jobs."""

    diagnostic_dir: str | None = None
    has_screenshot: bool = False
    has_html_dump: bool = False
    has_console_log: bool = False


class JobResponse(BaseModel):
    """Response with job information."""

    job_id: str
    status: str
    provider: str
    meeting_code: str
    display_name: str
    duration_sec: int
    attempt_no: int = 1
    retry_count: int = 0
    created_at: str
    started_at: str | None = None
    joined_at: str | None = None
    recording_started_at: str | None = None
    completed_at: str | None = None
    output_path: str | None = None
    raw_output_path: str | None = None
    trimmed_output_path: str | None = None
    file_size: int | None = None
    duration_actual_sec: float | None = None
    local_recording_deleted_at: str | None = None
    local_recording_cleanup_reason: str | None = None
    trim_start_sec: float | None = None
    trim_end_sec: float | None = None
    trim_status: str | None = None
    trim_reason: str | None = None
    dynamic_extension_stop_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    failure_stage: str | None = None
    runtime_summary: dict | None = None
    diagnostics: DiagnosticInfo | None = None


class ActiveJobPayload(BaseModel):
    """Active recording item in the /jobs/active response."""

    job_id: str
    status: str
    meeting_code: str
    display_name: str
    duration_sec: int
    started_at: str | None = None
    recording_started_at: str | None = None
    detectors: dict = Field(default_factory=dict)


class QueuedJobPayload(BaseModel):
    """FIFO queued item in the /jobs/active response."""

    kind: str
    queue_position: int
    job_id: str | None = None
    schedule_id: int | None = None
    status: str
    meeting_code: str | None = None
    display_name: str | None = None
    manual_trigger: bool = False
    created_at: str | None = None


class RetryWaitingJobPayload(BaseModel):
    """Delayed retry waiting item in the /jobs/active response."""

    job_id: str
    schedule_id: int | None = None
    status: str
    retry_after_sec: int
    meeting_code: str | None = None
    display_name: str | None = None


class ActiveRecordingsResponse(BaseModel):
    """Capacity and runtime state for currently active or queued recordings."""

    active: bool
    active_jobs: list[ActiveJobPayload]
    active_count: int
    queued_items: list[QueuedJobPayload] = Field(default_factory=list)
    retry_waiting_items: list[RetryWaitingJobPayload] = Field(default_factory=list)
    retry_waiting_count: int = 0
    queue_length: int
    max_concurrent_recordings: int
    available_slots: int


def _model_to_response(job: RecordingJobModel) -> JobResponse:
    """Convert database model to response."""
    diagnostics = None
    if job.diagnostic_dir:
        diagnostics = DiagnosticInfo(
            diagnostic_dir=job.diagnostic_dir,
            has_screenshot=job.has_screenshot,
            has_html_dump=job.has_html_dump,
            has_console_log=job.has_console_log,
        )

    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        provider=job.provider,
        meeting_code=job.meeting_code,
        display_name=job.display_name,
        duration_sec=job.duration_sec,
        attempt_no=job.attempt_no,
        retry_count=job.retry_count,
        created_at=job.created_at.isoformat() if job.created_at else "",
        started_at=job.started_at.isoformat() if job.started_at else None,
        joined_at=job.joined_at.isoformat() if job.joined_at else None,
        recording_started_at=job.recording_started_at.isoformat() if job.recording_started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        output_path=job.output_path,
        raw_output_path=job.raw_output_path,
        trimmed_output_path=job.trimmed_output_path,
        file_size=job.file_size,
        duration_actual_sec=job.duration_actual_sec,
        local_recording_deleted_at=job.local_recording_deleted_at.isoformat()
        if job.local_recording_deleted_at
        else None,
        local_recording_cleanup_reason=job.local_recording_cleanup_reason,
        trim_start_sec=job.trim_start_sec,
        trim_end_sec=job.trim_end_sec,
        trim_status=job.trim_status,
        trim_reason=job.trim_reason,
        dynamic_extension_stop_reason=job.dynamic_extension_stop_reason,
        error_code=job.error_code,
        error_message=job.error_message,
        failure_stage=job.failure_stage,
        runtime_summary=job.runtime_summary,
        diagnostics=diagnostics,
    )


@router.post("/record", response_model=JobResponse)
async def start_recording(
    request: RecordRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Start a new recording job.

    This endpoint starts a recording job in the background and returns immediately.
    Use GET /jobs/{job_id} to check the job status.
    """
    try:
        db_job = await get_app_job_service(http_request).start_immediate_recording(
            db,
            ImmediateRecordingData(
                provider=request.provider,
                meeting_code=request.meeting_code,
                display_name=request.display_name,
                duration_sec=request.duration_sec,
                base_url=request.base_url,
                password=request.password,
                lobby_wait_sec=request.lobby_wait_sec,
            ),
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _model_to_response(db_job)


@router.get("/current")
async def get_current_recording(http_request: Request, db: Session = Depends(get_db)):
    """Get currently active recording status for dashboard."""
    worker = get_app_worker(http_request)
    runner = get_app_job_runner(http_request)
    snapshot = get_app_job_runtime_state_service(http_request).build_snapshot(db, worker=worker, runner=runner)
    jobs = snapshot.active_jobs

    if not jobs and getattr(worker, "is_busy", False) and getattr(worker, "_current_job", None):
        repo = JobRepository(db)
        db_job = repo.get_by_job_id(worker._current_job.job_id)
        jobs = [db_job] if db_job and db_job.status in ACTIVE_RECORDING_STATUSES else []

    if not jobs:
        return {"active": False, "job": None, "active_count": 0}

    db_job = jobs[0]

    return {
        "active": True,
        "job": active_job_payload(db_job),
        "active_count": len(jobs),
    }


@router.get("/active", response_model=ActiveRecordingsResponse)
async def get_active_recordings(http_request: Request, db: Session = Depends(get_db)):
    """Get all active recording jobs and queue capacity state."""
    worker = get_app_worker(http_request)
    runner = get_app_job_runner(http_request)
    snapshot = get_app_job_runtime_state_service(http_request).build_snapshot(db, worker=worker, runner=runner)
    return snapshot.to_active_response()


@router.get("/progress/active")
async def get_active_progress(db: Session = Depends(get_db)):
    """Get latest compression/upload progress."""
    latest = get_latest_progress()
    if not latest:
        return {"active": False, "job": None, "progress": None}

    job_id, info = latest
    repo = JobRepository(db)
    job = repo.get_by_job_id(job_id)
    job_info = None
    if job:
        job_info = {
            "job_id": job.job_id,
            "meeting_code": job.meeting_code,
            "display_name": job.display_name,
        }

    return {
        "active": True,
        "job": job_info,
        "progress": {
            "phase": info.phase,
            "percent": info.percent,
            "current": info.current,
            "total": info.total,
            "unit": info.unit,
            "updated_at": info.updated_at.isoformat(),
        },
    }


@router.get("/{job_id}/progress")
async def get_job_progress(job_id: str):
    """Get compression/upload progress for a job."""
    info = get_progress(job_id)
    if not info:
        return {"active": False, "progress": None}

    return {
        "active": True,
        "progress": {
            "phase": info.phase,
            "percent": info.percent,
            "current": info.current,
            "total": info.total,
            "unit": info.unit,
            "updated_at": info.updated_at.isoformat(),
        },
    }


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, db: Session = Depends(get_db)):
    """Get job status and details."""
    repo = JobRepository(db)
    job = repo.get_by_job_id(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return _model_to_response(job)


@router.post("/{job_id}/stop")
async def stop_job(job_id: str, http_request: Request, db: Session = Depends(get_db)):
    """Request to stop a running job."""
    try:
        result = get_app_job_action_service(http_request).stop_job(db, job_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"message": result.message, "job_id": job_id}


@router.post("/{job_id}/finish")
async def finish_job(job_id: str, http_request: Request, db: Session = Depends(get_db)):
    """Request to finish a running job early (success path)."""
    try:
        result = get_app_job_action_service(http_request).finish_job(db, job_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"message": result.message, "job_id": job_id}


@router.get("/", response_model=list[JobResponse])
async def list_jobs(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List all jobs with pagination."""
    repo = JobRepository(db)
    jobs = repo.get_all(limit=limit, offset=offset)
    return [_model_to_response(job) for job in jobs]


@router.get("/{job_id}/diagnostics")
async def get_diagnostics(job_id: str, db: Session = Depends(get_db)):
    """Get diagnostic file paths for a failed job."""
    repo = JobRepository(db)
    job = repo.get_by_job_id(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if not job.diagnostic_dir:
        raise HTTPException(
            status_code=404,
            detail="No diagnostics available for this job",
        )

    from pathlib import Path

    diag_dir = Path(job.diagnostic_dir)

    files = {}
    if job.has_screenshot and (diag_dir / "screenshot.png").exists():
        files["screenshot"] = str(diag_dir / "screenshot.png")
    if job.has_html_dump and (diag_dir / "page.html").exists():
        files["html"] = str(diag_dir / "page.html")
    if job.has_console_log and (diag_dir / "console.log").exists():
        files["console_log"] = str(diag_dir / "console.log")
    if (diag_dir / "metadata.json").exists():
        files["metadata"] = str(diag_dir / "metadata.json")

    return {
        "job_id": job_id,
        "diagnostic_dir": job.diagnostic_dir,
        "files": files,
    }
