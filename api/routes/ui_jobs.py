"""Web UI routes for recording jobs."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from api.routes import ui_common, ui_job_diagnostics
from api.runtime import get_app_worker
from database.models import JobStatus, RecordingJob
from database.session import get_db

router = APIRouter(tags=["ui"])


def _mark_trimmed_artifact_state(job: RecordingJob) -> None:
    """Attach a display flag for trimmed files deleted after upload."""
    trimmed_output_path = getattr(job, "trimmed_output_path", None)
    job.trimmed_artifact_removed = bool(trimmed_output_path and not Path(trimmed_output_path).exists())


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Jobs list page."""
    # Define status groups for simplified filtering
    active_statuses = [
        JobStatus.QUEUED,
        JobStatus.STARTING,
        JobStatus.JOINING,
        JobStatus.WAITING_LOBBY,
        JobStatus.RECORDING,
        JobStatus.FINALIZING,
        JobStatus.UPLOADING,
    ]

    query = db.query(RecordingJob).order_by(RecordingJob.created_at.desc())

    # Apply simplified filter
    if status == "active":
        query = query.filter(RecordingJob.status.in_([s.value for s in active_statuses]))
    elif status == "succeeded":
        query = query.filter(RecordingJob.status == JobStatus.SUCCEEDED.value)
    elif status == "failed":
        query = query.filter(RecordingJob.status.in_([JobStatus.FAILED.value, JobStatus.CANCELED.value]))

    jobs = query.limit(50).all()

    # Find currently running job from database
    current_job = (
        db.query(RecordingJob)
        .filter(RecordingJob.status.in_([s.value for s in active_statuses]))
        .order_by(RecordingJob.created_at.desc())
        .first()
    )
    current_job_id = current_job.job_id if current_job else None

    return ui_common.render_template(
        request,
        "jobs/list.html",
        jobs=jobs,
        current_job_id=current_job_id,
        selected_status=status,
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def jobs_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Job detail page."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _mark_trimmed_artifact_state(job)
    status_value = job.status.value if hasattr(job.status, "value") else job.status
    failure_context = None
    job_logs: list[ui_job_diagnostics.JobLogView] = []
    if status_value in {JobStatus.FAILED.value, JobStatus.CANCELED.value}:
        failure_context = ui_job_diagnostics._load_failure_context(job)
        job_logs = ui_job_diagnostics._load_job_logs(job)

    return ui_common.render_template(
        request,
        "jobs/detail.html",
        job=job,
        failure_context=failure_context,
        job_logs=job_logs,
    )


@router.post("/jobs/{job_id}/stop", response_class=HTMLResponse)
async def jobs_stop(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Stop a running job."""
    # Find the job and check if it's running
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        running_statuses = [
            JobStatus.STARTING.value,
            JobStatus.JOINING.value,
            JobStatus.WAITING_LOBBY.value,
            JobStatus.RECORDING.value,
            JobStatus.FINALIZING.value,
            JobStatus.UPLOADING.value,
        ]
        if job.status in running_statuses:
            worker = get_app_worker(request)
            if worker.is_busy and worker._current_job and worker._current_job.job_id == job_id:
                # Worker is running this job - request cancellation
                worker.request_cancel()
            else:
                # Orphaned job - worker is not running it, update DB directly
                job.status = JobStatus.CANCELED.value
                job.error_message = "Stopped by user (job was orphaned)"
                db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/finish", response_class=HTMLResponse)
async def jobs_finish(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Finish a running job early (success path)."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        running_statuses = [
            JobStatus.STARTING.value,
            JobStatus.JOINING.value,
            JobStatus.WAITING_LOBBY.value,
            JobStatus.RECORDING.value,
            JobStatus.FINALIZING.value,
            JobStatus.UPLOADING.value,
        ]
        if job.status in running_statuses:
            worker = get_app_worker(request)
            if worker.is_busy and worker._current_job and worker._current_job.job_id == job_id:
                # Worker is running this job - request finish
                worker.request_finish()
            else:
                # Orphaned job - worker is not running it, update DB directly
                job.status = JobStatus.CANCELED.value
                job.error_message = "Finished by user (job was orphaned)"
                db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.delete("/jobs", response_class=HTMLResponse)
async def jobs_delete_all(db: Session = Depends(get_db)):
    """Delete all jobs."""
    db.query(RecordingJob).delete()
    db.commit()
    return HTMLResponse("")


@router.delete("/jobs/{job_id}", response_class=HTMLResponse)
async def jobs_delete(job_id: str, db: Session = Depends(get_db)):
    """Delete job."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        db.delete(job)
        db.commit()
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
