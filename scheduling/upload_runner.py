"""YouTube upload orchestration for completed recordings."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from config.settings import get_settings
from database.models import JobStatus
from database.session import JobRepository, get_session_local
from recording.remux import ensure_mp4
from telegram_bot.notifications import notify_youtube_upload_completed
from uploading.progress import clear_progress, update_progress
from uploading.youtube import UploadStatus, VideoMetadata, get_youtube_uploader
from utils.timezone import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadRequest:
    job_id: str
    video_path: Path
    title: str
    privacy: str
    meeting_name: str | None = None
    raw_video_path: Path | None = None
    cleanup_video_path_after_success: Path | None = None


class YouTubeUploadRunner:
    """Prepare recordings and upload them to YouTube one at a time."""

    def __init__(self) -> None:
        self._upload_lock = asyncio.Lock()

    async def run_upload_task(self, upload_request: UploadRequest) -> None:
        upload_job_id = upload_request.job_id
        try:
            video_path = upload_request.video_path
            remux_log = None
            transcode_log = None
            if video_path.suffix.lower() == ".mkv":
                settings = get_settings()
                remux_log = settings.diagnostics_dir / upload_request.job_id / "remux.log"
                transcode_log = settings.diagnostics_dir / upload_request.job_id / "transcode.log"

            def transcode_progress(current_ms: int | None, total_ms: int | None) -> None:
                update_progress(upload_job_id, "compressing", current_ms, total_ms, "ms")

            upload_path = await ensure_mp4(
                video_path,
                remux_log_path=remux_log,
                transcode_log_path=transcode_log,
                progress_callback=transcode_progress,
            )
            if not upload_path or not upload_path.exists():
                logger.error(f"MP4 preparation failed, skipping YouTube upload for job {upload_request.job_id}")
                return

            async with self._upload_lock:
                upload_succeeded = await self.upload_to_youtube(
                    job_id=upload_request.job_id,
                    video_path=upload_path,
                    title=upload_request.title,
                    privacy=upload_request.privacy,
                )
                if upload_succeeded and upload_request.cleanup_video_path_after_success:
                    await self._cleanup_uploaded_trimmed_output(
                        job_id=upload_request.job_id,
                        cleanup_path=upload_request.cleanup_video_path_after_success,
                        prepared_upload_path=upload_path,
                        raw_video_path=upload_request.raw_video_path,
                    )
        except Exception as e:
            logger.error(f"YouTube upload task failed for job {upload_request.job_id}: {e}")
        finally:
            clear_progress(upload_job_id)

    async def upload_to_youtube(
        self,
        *,
        job_id: str,
        video_path: Path,
        title: str,
        privacy: str = "unlisted",
    ) -> bool:
        """Upload recording to YouTube."""
        logger.info(f"Starting YouTube upload for job {job_id}")
        try:
            file_size = video_path.stat().st_size
        except OSError:
            file_size = None
        update_progress(job_id, "uploading", 0, file_size, "bytes")

        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            repo.update_status(job_id, JobStatus.UPLOADING.value)
            session.commit()
        finally:
            session.close()

        uploader = get_youtube_uploader()
        if not uploader.is_configured:
            logger.warning("YouTube upload skipped - not configured")
            return False
        if not uploader.is_authorized:
            logger.warning("YouTube upload skipped - not authorized")
            return False

        try:
            metadata = VideoMetadata(
                title=title,
                description=f"Recorded meeting - {job_id}",
                privacy_status=privacy,
            )

            def log_progress(uploaded: int, total: int) -> None:
                percent = (uploaded / total * 100) if total > 0 else 0
                logger.debug(f"Upload progress: {percent:.1f}% ({uploaded}/{total} bytes)")
                update_progress(job_id, "uploading", uploaded, total, "bytes")

            result = await uploader.upload_video(
                video_path=video_path,
                metadata=metadata,
                progress_callback=log_progress,
            )

            session = SessionLocal()
            try:
                repo = JobRepository(session)
                if result.status == UploadStatus.SUCCEEDED:
                    repo.update_status(
                        job_id,
                        JobStatus.SUCCEEDED.value,
                        youtube_video_id=result.video_id,
                        youtube_uploaded_at=utc_now(),
                    )
                    logger.info(f"YouTube upload successful: {result.video_url}")
                    session.commit()

                    db_job = repo.get_by_job_id(job_id)
                    if db_job and result.video_url:
                        await notify_youtube_upload_completed(db_job, result.video_url)
                    return True
                else:
                    logger.error(f"YouTube upload failed: {result.error_message}")
                    session.commit()
                    return False
            finally:
                session.close()
        except Exception as e:
            logger.error(f"YouTube upload error: {e}")
            return False

    async def _cleanup_uploaded_trimmed_output(
        self,
        *,
        job_id: str,
        cleanup_path: Path,
        prepared_upload_path: Path,
        raw_video_path: Path | None,
    ) -> None:
        """Delete local trimmed upload artifacts after a successful upload."""
        for candidate in {cleanup_path, prepared_upload_path}:
            try:
                if candidate.exists() and (raw_video_path is None or candidate != raw_video_path):
                    candidate.unlink()
                    logger.info("Deleted uploaded trimmed artifact: %s", candidate)
            except OSError as exc:
                logger.warning("Failed to delete uploaded trimmed artifact %s: %s", candidate, exc)

        if raw_video_path and raw_video_path.exists():
            SessionLocal = get_session_local()
            session = SessionLocal()
            try:
                repo = JobRepository(session)
                repo.update_status(job_id, JobStatus.SUCCEEDED.value, output_path=str(raw_video_path))
                session.commit()
            finally:
                session.close()
