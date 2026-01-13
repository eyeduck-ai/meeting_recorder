from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import (
    JobStatus,
    get_db,
    init_db,
)
from database.models import (
    RecordingJob as RecordingJobModel,
)
from database.session import JobRepository, build_result_update_fields
from recording.worker import (
    RecordingJob,
    get_worker,
)
from telegram_bot.notifications import (
    notify_recording_completed,
    notify_recording_failed,
    notify_recording_started,
)

router = APIRouter(prefix="/jobs", tags=["Jobs"])

# Initialize database on module load
init_db()


class RecordRequest(BaseModel):
    """Request to start a recording."""

    provider: Literal["jitsi", "webex"] = "jitsi"
    meeting_code: str = Field(..., min_length=1, description="Meeting room code or full URL")
    display_name: str = Field(default="Recorder Bot", description="Display name in meeting")
    duration_sec: int = Field(default=3600, ge=60, le=14400, description="Recording duration in seconds")
    base_url: str | None = Field(default=None, description="Custom base URL for provider")
    password: str | None = Field(default=None, description="Meeting password if required")
    lobby_wait_sec: int = Field(default=900, ge=0, le=1800, description="Max lobby wait time")


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
    created_at: str
    started_at: str | None = None
    joined_at: str | None = None
    recording_started_at: str | None = None
    completed_at: str | None = None
    output_path: str | None = None
    file_size: int | None = None
    duration_actual_sec: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    diagnostics: DiagnosticInfo | None = None


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
        created_at=job.created_at.isoformat() if job.created_at else "",
        started_at=job.started_at.isoformat() if job.started_at else None,
        joined_at=job.joined_at.isoformat() if job.joined_at else None,
        recording_started_at=job.recording_started_at.isoformat() if job.recording_started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        output_path=job.output_path,
        file_size=job.file_size,
        duration_actual_sec=job.duration_actual_sec,
        error_code=job.error_code,
        error_message=job.error_message,
        diagnostics=diagnostics,
    )


async def _run_recording(job: RecordingJob, db_job_id: int) -> None:
    """Background task to run recording."""
    import asyncio

    from database.models import get_session_local

    worker = get_worker()
    SessionLocal = get_session_local()

    def on_status_change(job_id: str, status: JobStatus):
        """Update status in database and send notifications."""
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            update_fields = {"status": status.value}

            # Add timestamps for specific statuses
            if status == JobStatus.STARTING:
                update_fields["started_at"] = datetime.now()
            elif status == JobStatus.RECORDING:
                update_fields["recording_started_at"] = datetime.now()

            repo.update_status(job_id, status.value, **{k: v for k, v in update_fields.items() if k != "status"})
            session.commit()

            # Send start notification when recording begins
            if status == JobStatus.RECORDING:
                db_job = repo.get_by_job_id(job_id)
                if db_job:
                    asyncio.create_task(notify_recording_started(db_job))
        finally:
            session.close()

    worker.set_status_callback(on_status_change)

    # Run recording
    result = await worker.record(job)

    # Update database with final result
    session = SessionLocal()
    try:
        repo = JobRepository(session)
        update_fields = build_result_update_fields(result)

        repo.update_status(job.job_id, result.status.value, **update_fields)
        session.commit()

        # Send completion/failure notification
        db_job = repo.get_by_job_id(job.job_id)
        if db_job:
            if result.status == JobStatus.SUCCEEDED:
                await notify_recording_completed(db_job)
            elif result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                await notify_recording_failed(db_job)
    finally:
        session.close()


@router.post("/record", response_model=JobResponse)
async def start_recording(
    request: RecordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Start a new recording job.

    This endpoint starts a recording job in the background and returns immediately.
    Use GET /jobs/{job_id} to check the job status.
    """
    worker = get_worker()

    # Check if worker is busy
    if worker.is_busy:
        raise HTTPException(
            status_code=409,
            detail="Worker is busy with another recording. Only one recording at a time is supported.",
        )

    # Create job
    job = RecordingJob.create(
        provider=request.provider,
        meeting_code=request.meeting_code,
        display_name=request.display_name,
        duration_sec=request.duration_sec,
        base_url=request.base_url,
        password=request.password,
        lobby_wait_sec=request.lobby_wait_sec,
    )

    # Store in database
    repo = JobRepository(db)
    db_job = repo.create(
        job_id=job.job_id,
        provider=job.provider,
        meeting_code=job.meeting_code,
        display_name=job.display_name,
        base_url=job.base_url,
        duration_sec=job.duration_sec,
        lobby_wait_sec=job.lobby_wait_sec,
        status=JobStatus.QUEUED.value,
    )
    db.commit()

    # Start recording in background
    background_tasks.add_task(_run_recording, job, db_job.id)

    return _model_to_response(db_job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, db: Session = Depends(get_db)):
    """Get job status and details."""
    repo = JobRepository(db)
    job = repo.get_by_job_id(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return _model_to_response(job)


@router.post("/{job_id}/stop")
async def stop_job(job_id: str, db: Session = Depends(get_db)):
    """Request to stop a running job."""
    repo = JobRepository(db)
    job = repo.get_by_job_id(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check if job can be stopped
    terminal_statuses = [
        JobStatus.SUCCEEDED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELED.value,
    ]
    if job.status in terminal_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {job.status}",
        )

    worker = get_worker()
    if worker.request_cancel():
        return {"message": "Cancellation requested", "job_id": job_id}
    else:
        raise HTTPException(
            status_code=400,
            detail="Could not request cancellation",
        )


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


@router.get("/current")
async def get_current_recording(db: Session = Depends(get_db)):
    """Get currently active recording status for dashboard."""
    worker = get_worker()
    
    # Check if worker is busy
    if not worker.is_busy or not worker._current_job:
        return {"active": False, "job": None}
    
    # Get database record for the current job
    repo = JobRepository(db)
    current_job_id = worker._current_job.job_id
    db_job = repo.get_by_job_id(current_job_id)
    
    if not db_job:
        return {"active": False, "job": None}
    
    # Build response with live status
    return {
        "active": True,
        "job": {
            "job_id": db_job.job_id,
            "status": db_job.status,
            "meeting_code": db_job.meeting_code,
            "display_name": db_job.display_name,
            "duration_sec": db_job.duration_sec,
            "started_at": db_job.started_at.isoformat() if db_job.started_at else None,
            "recording_started_at": db_job.recording_started_at.isoformat() if db_job.recording_started_at else None,
            # Detector status placeholder - will be populated when detection is active
            "detectors": {},
        }
    }


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
