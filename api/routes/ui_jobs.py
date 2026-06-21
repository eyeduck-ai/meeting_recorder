"""Web UI routes for recording jobs."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from api.routes import ui_common, ui_job_diagnostics, ui_recording_artifacts
from api.runtime import (
    get_app_job_action_service,
    get_app_job_runner,
    get_app_job_runtime_state_service,
    get_app_worker,
)
from database.models import JobStatus, RecordingJob
from database.session import get_db
from services.errors import NotFoundError, ServiceError, ValidationError
from services.job_actions import ACTIVE_RECORDING_STATUSES, TERMINAL_JOB_STATUSES, job_status_value

router = APIRouter(tags=["ui"])

ACTIVE_FILTER_STATUSES = {
    JobStatus.QUEUED.value,
    *ACTIVE_RECORDING_STATUSES,
    JobStatus.UPLOADING.value,
}


def _raise_http_error(exc: Exception) -> None:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ServiceError):
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    raise exc


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Jobs list page."""
    query = db.query(RecordingJob).order_by(RecordingJob.created_at.desc())

    # Apply simplified filter
    if status == "active":
        query = query.filter(RecordingJob.status.in_(ACTIVE_FILTER_STATUSES))
    elif status == "succeeded":
        query = query.filter(RecordingJob.status == JobStatus.SUCCEEDED.value)
    elif status == "failed":
        query = query.filter(RecordingJob.status.in_([JobStatus.FAILED.value, JobStatus.CANCELED.value]))

    jobs = query.limit(50).all()

    worker = get_app_worker(request)
    runner = get_app_job_runner(request)
    runtime_snapshot = get_app_job_runtime_state_service(request).build_snapshot(db, worker=worker, runner=runner)
    has_terminal_jobs = (
        db.query(RecordingJob.id).filter(RecordingJob.status.in_(TERMINAL_JOB_STATUSES)).first() is not None
    )

    return ui_common.render_template(
        request,
        "jobs/list.html",
        jobs=jobs,
        active_job_ids=runtime_snapshot.active_job_ids,
        queued_job_ids=runtime_snapshot.queued_job_ids,
        queued_positions_by_job_id=runtime_snapshot.queued_positions_by_job_id,
        retry_waiting_job_ids=runtime_snapshot.retry_waiting_job_ids,
        retry_after_by_job_id=runtime_snapshot.retry_after_by_job_id,
        queued_schedule_items=runtime_snapshot.queued_schedule_items,
        queued_items=runtime_snapshot.queued_items,
        retry_waiting_items=runtime_snapshot.retry_waiting_items,
        terminal_job_statuses=TERMINAL_JOB_STATUSES,
        has_terminal_jobs=has_terminal_jobs,
        selected_status=status,
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def jobs_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Job detail page."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    ui_recording_artifacts.mark_trimmed_artifact_state(job)
    status_value = job_status_value(job)
    failure_context = None
    job_logs: list[ui_job_diagnostics.JobLogView] = []
    if status_value in {JobStatus.FAILED.value, JobStatus.CANCELED.value}:
        failure_context = ui_job_diagnostics._load_failure_context(job)
        job_logs = ui_job_diagnostics._load_job_logs(job)
    worker = get_app_worker(request)
    runner = get_app_job_runner(request)
    runtime_snapshot = get_app_job_runtime_state_service(request).build_snapshot(db, worker=worker, runner=runner)

    return ui_common.render_template(
        request,
        "jobs/detail.html",
        job=job,
        failure_context=failure_context,
        job_logs=job_logs,
        can_control_job=job_id in runtime_snapshot.active_job_ids and status_value in ACTIVE_RECORDING_STATUSES,
        is_queued_job=job_id in runtime_snapshot.queued_job_ids,
        is_retry_waiting_job=job_id in runtime_snapshot.retry_after_by_job_id,
        retry_after_sec=runtime_snapshot.retry_after_by_job_id.get(job_id),
        can_delete_job=status_value in TERMINAL_JOB_STATUSES,
    )


@router.post("/jobs/{job_id}/stop", response_class=HTMLResponse)
async def jobs_stop(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Stop a running job."""
    try:
        get_app_job_action_service(request).stop_job(db, job_id)
    except Exception as exc:
        _raise_http_error(exc)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/finish", response_class=HTMLResponse)
async def jobs_finish(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Finish a running job early (success path)."""
    try:
        get_app_job_action_service(request).finish_job(db, job_id)
    except Exception as exc:
        _raise_http_error(exc)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.delete("/jobs", response_class=HTMLResponse)
async def jobs_delete_all(request: Request, db: Session = Depends(get_db)):
    """Delete terminal jobs."""
    try:
        get_app_job_action_service(request).delete_terminal_jobs(db)
    except Exception as exc:
        _raise_http_error(exc)
    return HTMLResponse("")


@router.delete("/jobs/{job_id}", response_class=HTMLResponse)
async def jobs_delete(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Delete job."""
    try:
        get_app_job_action_service(request).delete_job(db, job_id)
    except Exception as exc:
        _raise_http_error(exc)
    return HTMLResponse("")


@router.get("/jobs/{job_id}/diagnostics/screenshot", response_class=FileResponse)
async def jobs_screenshot(job_id: str, db: Session = Depends(get_db)):
    """Get job diagnostic screenshot."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job or not job.diagnostic_dir or not job.has_screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    screenshot_path = Path(job.diagnostic_dir) / "screenshot.png"
    if not screenshot_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return FileResponse(screenshot_path, media_type="image/png")


@router.get("/jobs/{job_id}/logs/{log_name}", response_class=PlainTextResponse)
async def jobs_log(job_id: str, log_name: str, db: Session = Depends(get_db)):
    """Get a per-job diagnostic log as plain text."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    log_path = ui_job_diagnostics._resolve_job_log_path(job, log_name)
    if not log_path:
        raise HTTPException(status_code=404, detail="Log not found")

    try:
        return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to read log") from exc
