"""YouTube upload orchestration for completed recordings."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from config.settings import get_settings
from database.models import JobStatus
from database.session import JobRepository, get_session_local
from recording.mp4_validation import discard_file
from services.storage_maintenance import CanonicalRecording, prepare_upload_recording_file
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
        self._transcode_sem = asyncio.Semaphore(max(1, int(getattr(get_settings(), "max_parallel_transcodes", 1))))
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

            async with self._transcode_sem:
                canonical = await prepare_upload_recording_file(
                    video_path,
                    remux_log_path=remux_log,
                    transcode_log_path=transcode_log,
                    progress_callback=transcode_progress,
                )
            if not canonical or not canonical.output_path.exists():
                logger.error(f"MP4 preparation failed, skipping YouTube upload for job {upload_request.job_id}")
                self._mark_upload_completed_without_video(
                    upload_request.job_id,
                    "YouTube upload skipped: MP4 preparation failed",
                )
                return
            self._persist_local_recording_path(upload_request.job_id, canonical)
            upload_path = canonical.upload_path or canonical.output_path

            try:
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
                            prepared_upload_path=canonical.output_path,
                            raw_video_path=upload_request.raw_video_path,
                        )
            finally:
                if canonical.temporary_upload_path:
                    discard_file(canonical.temporary_upload_path)
        except Exception as e:
            logger.error(f"YouTube upload task failed for job {upload_request.job_id}: {e}")
            self._mark_upload_completed_without_video(
                upload_request.job_id,
                f"YouTube upload failed: {e}",
            )
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
        uploader = get_youtube_uploader()
        if not uploader.is_configured:
            logger.warning("YouTube upload skipped - not configured")
            self._mark_upload_completed_without_video(job_id, "YouTube upload skipped: not configured")
            clear_progress(job_id)
            return False
        if not uploader.is_authorized:
            logger.warning("YouTube upload skipped - not authorized")
            self._mark_upload_completed_without_video(job_id, "YouTube upload skipped: not authorized")
            clear_progress(job_id)
            return False

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
                        error_message=None,
                    )
                    logger.info(f"YouTube upload successful: {result.video_url}")
                    session.commit()

                    db_job = repo.get_by_job_id(job_id)
                    if db_job and result.video_url:
                        try:
                            await notify_youtube_upload_completed(db_job, result.video_url)
                        except Exception as e:
                            logger.warning("Failed to send YouTube upload notification for job %s: %s", job_id, e)
                    return True
                else:
                    logger.error(f"YouTube upload failed: {result.error_message}")
                    repo.update_status(
                        job_id,
                        JobStatus.SUCCEEDED.value,
                        error_message=f"YouTube upload failed: {result.error_message or 'unknown error'}",
                    )
                    session.commit()
                    clear_progress(job_id)
                    return False
            finally:
                session.close()
        except Exception as e:
            logger.error(f"YouTube upload error: {e}")
            self._mark_upload_completed_without_video(job_id, f"YouTube upload failed: {e}")
            clear_progress(job_id)
            return False

    def _persist_local_recording_path(self, job_id: str, canonical: CanonicalRecording) -> None:
        """Persist the canonical local recording path after MP4 preparation."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            repo.update_status(
                job_id,
                JobStatus.SUCCEEDED.value,
                output_path=str(canonical.output_path),
                file_size=canonical.file_size,
            )
            session.commit()
        finally:
            session.close()

    def _mark_upload_completed_without_video(self, job_id: str, message: str) -> None:
        """Return a recording-successful job from upload processing to a terminal state."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            repo.update_status(
                job_id,
                JobStatus.SUCCEEDED.value,
                error_message=message,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to restore job %s after upload issue", job_id)
        finally:
            session.close()

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
