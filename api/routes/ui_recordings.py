"""Web UI routes for completed recordings."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.routes import ui_common, ui_recording_artifacts
from database.models import JobStatus, RecordingJob
from database.session import get_db
from recording.remux import delete_recording_artifacts

router = APIRouter(tags=["ui"])
logger = logging.getLogger(__name__)


def _delete_job_recording_files(job: RecordingJob) -> None:
    """Best-effort delete all local files for a completed job."""
    candidates = [
        Path(output_path) if output_path else None
        for output_path in (job.output_path, job.raw_output_path, job.trimmed_output_path)
    ]
    _deleted, errors = delete_recording_artifacts(candidates)
    for candidate, exc in errors:
        logger.warning("Error deleting file %s for %s: %s", candidate, job.job_id, exc)


@router.get("/recordings", response_class=HTMLResponse)
async def recordings_list(request: Request, db: Session = Depends(get_db)):
    """Recordings list page."""
    from uploading.youtube import get_youtube_uploader

    jobs = (
        db.query(RecordingJob)
        .filter(
            RecordingJob.status == JobStatus.SUCCEEDED,
            or_(RecordingJob.output_path != None, RecordingJob.youtube_video_id != None),
        )
        .order_by(RecordingJob.completed_at.desc())
        .all()
    )
    for job in jobs:
        ui_recording_artifacts.mark_recording_artifact_state(job)

    # Get YouTube status for upload button visibility
    uploader = get_youtube_uploader()
    youtube_configured = ui_common.settings.youtube_configured
    youtube_authorized = uploader.is_authorized if youtube_configured else False

    return ui_common.render_template(
        request,
        "recordings/list.html",
        jobs=jobs,
        youtube_configured=youtube_configured,
        youtube_authorized=youtube_authorized,
    )


@router.delete("/recordings", response_class=HTMLResponse)
async def recordings_delete_all(db: Session = Depends(get_db)):
    """Delete all recordings (files and database records)."""
    jobs = (
        db.query(RecordingJob)
        .filter(
            RecordingJob.status == JobStatus.SUCCEEDED,
            or_(RecordingJob.output_path != None, RecordingJob.youtube_video_id != None),
        )
        .all()
    )

    for job in jobs:
        _delete_job_recording_files(job)
        db.delete(job)

    db.commit()
    return HTMLResponse("")


@router.get("/recordings/{job_id}/download")
async def recordings_download(job_id: str, db: Session = Depends(get_db)):
    """Download recording file."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = ui_recording_artifacts.preferred_existing_output(job)
    if not file_path:
        raise HTTPException(status_code=404, detail="Recording file not found")

    media_type = "video/mp4" if file_path.suffix.lower() == ".mp4" else "video/x-matroska"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=file_path.name,
    )


@router.delete("/recordings/{job_id}", response_class=HTMLResponse)
async def recordings_delete(job_id: str, db: Session = Depends(get_db)):
    """Delete recording file and job."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        _delete_job_recording_files(job)
        db.delete(job)
        db.commit()

    return HTMLResponse("")
