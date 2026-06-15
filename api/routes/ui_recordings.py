"""Web UI routes for completed recordings."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.routes import ui_common
from database.models import JobStatus, RecordingJob
from database.session import get_db
from recording.remux import pick_preferred_video_path

router = APIRouter(tags=["ui"])


def _recording_file_candidates(path: Path) -> set[Path]:
    """Return compatible recording file variants for cleanup."""
    candidates = {path}
    if path.suffix.lower() == ".mkv":
        candidates.add(path.with_suffix(".mp4"))
    elif path.suffix.lower() == ".mp4":
        candidates.add(path.with_suffix(".mkv"))
    return candidates


def _delete_recording_files(output_path: str) -> None:
    """Best-effort delete for the primary recording file and remuxed sibling."""
    file_path = Path(output_path)
    for candidate in _recording_file_candidates(file_path):
        if candidate.exists():
            candidate.unlink()


def _resolve_local_download_path(job: RecordingJob) -> Path | None:
    """Return the downloadable local recording path if it still exists."""
    if not job.output_path or job.local_recording_deleted_at:
        return None

    file_path = Path(job.output_path)
    if not file_path.exists():
        return None

    preferred_path = pick_preferred_video_path(file_path)
    return preferred_path if preferred_path.exists() else None


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
        job.local_download_available = _resolve_local_download_path(job) is not None

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
        # Try to delete file from disk if it exists
        if job.output_path:
            try:
                _delete_recording_files(job.output_path)
            except Exception as e:
                print(f"Error deleting file {job.output_path}: {e}")

        # Delete job from database
        db.delete(job)

    db.commit()
    return HTMLResponse("")


@router.get("/recordings/{job_id}/download")
async def recordings_download(job_id: str, db: Session = Depends(get_db)):
    """Download recording file."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = _resolve_local_download_path(job)
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
        # Try to delete file from disk if it exists
        if job.output_path:
            try:
                _delete_recording_files(job.output_path)
            except Exception as e:
                print(f"Error deleting file {job.output_path}: {e}")

        # Delete job from database
        db.delete(job)
        db.commit()

    return HTMLResponse("")
