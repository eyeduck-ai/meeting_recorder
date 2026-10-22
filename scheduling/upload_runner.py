"""YouTube upload orchestration for completed recordings."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from config.settings import get_settings
from database.models import JobStatus
from database.session import JobRepository, get_session_local
from recording.mp4_validation import discard_file
from recording.remux import delete_recording_artifacts, recording_file_variants
from services.recording_retention import preserve_raw_recording
from services.storage_maintenance import CanonicalRecording, prepare_upload_recording_file
from telegram_bot.notifications import notify_youtube_upload_completed
from uploading.progress import clear_progress, update_progress
from uploading.youtube import UploadResult, UploadStatus, VideoMetadata, get_youtube_uploader
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
    description: str | None = None


class YouTubeUploadRunner:
    """Prepare recordings and upload them to YouTube one at a time."""

    def __init__(self, *, durable_store=None, session_factory=None) -> None:
        self._transcode_sem = asyncio.Semaphore(max(1, int(getattr(get_settings(), "max_parallel_transcodes", 1))))
        self._upload_lock = asyncio.Lock()
        self._durable_store = durable_store
        self._session_factory = session_factory or (lambda: get_session_local())
        self.last_outcome: dict[str, UploadResult] = {}

    async def run_upload_task(self, upload_request: UploadRequest) -> bool:
        upload_job_id = upload_request.job_id
        self.last_outcome.pop(upload_job_id, None)
        try:
            if self._durable_store:
                confirmed_video_id = self._durable_store.resume_state(upload_job_id).get("video_id")
                if confirmed_video_id:
                    self._restore_confirmed_video(upload_job_id, confirmed_video_id)
            if self._durable_store and await self._already_uploaded(upload_job_id):
                resume = self._durable_store.resume_state(upload_job_id)
                if upload_request.cleanup_video_path_after_success:
                    await self._cleanup_uploaded_trimmed_output(
                        job_id=upload_job_id,
                        cleanup_path=upload_request.cleanup_video_path_after_success,
                        prepared_upload_path=Path(resume.get("local_path") or upload_request.video_path),
                        raw_video_path=upload_request.raw_video_path,
                    )
                if resume.get("temporary_upload_path"):
                    discard_file(Path(resume["temporary_upload_path"]))
                return True
            video_path = upload_request.video_path
            remux_log = None
            transcode_log = None
            if video_path.suffix.lower() == ".mkv":
                settings = get_settings()
                remux_log = settings.diagnostics_dir / upload_request.job_id / "remux.log"
                transcode_log = settings.diagnostics_dir / upload_request.job_id / "transcode.log"

            def transcode_progress(current_ms: int | None, total_ms: int | None) -> None:
                update_progress(upload_job_id, "compressing", current_ms, total_ms, "ms")

            resume = self._durable_store.resume_state(upload_job_id) if self._durable_store else {}
            if resume.get("session_uri") or resume.get("prepared_path"):
                # Retain the exact upload artifact across retries; re-encoding can change bytes.
                prepared = Path(resume["prepared_path"])
                canonical = CanonicalRecording(
                    output_path=Path(resume["local_path"]),
                    file_size=resume["local_file_size"],
                    upload_path=prepared,
                    temporary_upload_path=Path(resume["temporary_upload_path"])
                    if resume.get("temporary_upload_path")
                    else None,
                )
            else:
                async with self._transcode_sem:
                    canonical = await prepare_upload_recording_file(
                        video_path,
                        remux_log_path=remux_log,
                        transcode_log_path=transcode_log,
                        progress_callback=transcode_progress,
                        **(
                            {"remove_source": False}
                            if self._durable_store and self._preserve_raw(upload_job_id)
                            else {}
                        ),
                    )
                if self._durable_store and canonical:
                    resume.update(
                        prepared_path=str(canonical.upload_path or canonical.output_path),
                        local_path=str(canonical.output_path),
                        local_file_size=canonical.file_size,
                        temporary_upload_path=str(canonical.temporary_upload_path)
                        if canonical.temporary_upload_path
                        else None,
                    )
                    self._durable_store.save_resume_state(upload_job_id, resume)
            if not canonical or not canonical.output_path.exists():
                logger.error(f"MP4 preparation failed, skipping YouTube upload for job {upload_request.job_id}")
                self._mark_upload_completed_without_video(
                    upload_request.job_id,
                    "YouTube upload skipped: MP4 preparation failed",
                )
                return False
            self._persist_local_recording_path(upload_request.job_id, canonical)
            upload_path = canonical.upload_path or canonical.output_path

            upload_succeeded = False
            try:
                async with self._upload_lock:
                    upload_succeeded = await self.upload_to_youtube(
                        job_id=upload_request.job_id,
                        video_path=upload_path,
                        title=upload_request.title,
                        privacy=upload_request.privacy,
                        **(
                            {"description": upload_request.description}
                            if upload_request.description is not None
                            else {}
                        ),
                    )
                    if upload_succeeded and upload_request.cleanup_video_path_after_success:
                        await self._cleanup_uploaded_trimmed_output(
                            job_id=upload_request.job_id,
                            cleanup_path=upload_request.cleanup_video_path_after_success,
                            prepared_upload_path=canonical.output_path,
                            raw_video_path=upload_request.raw_video_path,
                        )
            finally:
                if canonical.temporary_upload_path and (upload_succeeded or not self._durable_store):
                    discard_file(canonical.temporary_upload_path)
            return upload_succeeded
        except Exception as e:
            logger.error(f"YouTube upload task failed for job {upload_request.job_id}: {e}")
            self._mark_upload_completed_without_video(
                upload_request.job_id,
                f"YouTube upload failed: {type(e).__name__}",
            )
            return False
        finally:
            clear_progress(upload_job_id)

    async def upload_to_youtube(
        self,
        *,
        job_id: str,
        video_path: Path,
        title: str,
        privacy: str = "unlisted",
        description: str | None = None,
    ) -> bool:
        """Upload recording to YouTube."""
        logger.info(f"Starting YouTube upload for job {job_id}")
        if self._durable_store and await self._already_uploaded(job_id):
            return True
        uploader = get_youtube_uploader()
        if not uploader.is_configured:
            logger.warning("YouTube upload skipped - not configured")
            self._mark_upload_completed_without_video(job_id, "YouTube upload skipped: not configured")
            self.last_outcome[job_id] = UploadResult(status=UploadStatus.FAILED, retryable=False)
            clear_progress(job_id)
            return False
        if not uploader.is_authorized:
            logger.warning("YouTube upload skipped - not authorized")
            self._mark_upload_completed_without_video(job_id, "YouTube upload skipped: not authorized")
            self.last_outcome[job_id] = UploadResult(status=UploadStatus.FAILED, retryable=False)
            clear_progress(job_id)
            return False

        try:
            file_size = video_path.stat().st_size
        except OSError:
            file_size = None
        update_progress(job_id, "uploading", 0, file_size, "bytes")

        SessionLocal = self._session_factory()
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
                description=description or f"Recorded meeting - {job_id}",
                privacy_status=privacy,
            )

            def log_progress(uploaded: int, total: int) -> None:
                percent = (uploaded / total * 100) if total > 0 else 0
                logger.debug(f"Upload progress: {percent:.1f}% ({uploaded}/{total} bytes)")
                update_progress(job_id, "uploading", uploaded, total, "bytes")

            durable_args = {}
            if self._durable_store:
                durable_args = {
                    "resume_state": self._durable_store.resume_state(job_id),
                    "state_callback": lambda state: self._durable_store.save_resume_state(job_id, state),
                }
            result = await uploader.upload_video(
                video_path=video_path,
                metadata=metadata,
                progress_callback=log_progress,
                **durable_args,
            )
            self.last_outcome[job_id] = result

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
                            message_id = await notify_youtube_upload_completed(db_job, result.video_url)
                            if message_id and message_id != db_job.telegram_message_id:
                                db_job.telegram_message_id = message_id
                                session.commit()
                        except Exception as e:
                            session.rollback()
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

    def _preserve_raw(self, job_id: str) -> bool:
        SessionLocal = self._session_factory()
        with SessionLocal() as session:
            job = JobRepository(session).get_by_job_id(job_id)
            return bool(job and preserve_raw_recording(job.runtime_summary_json))

    def _restore_confirmed_video(self, job_id: str, video_id: str) -> None:
        SessionLocal = self._session_factory()
        with SessionLocal() as session:
            job = JobRepository(session).get_by_job_id(job_id)
            if job and not job.youtube_video_id:
                job.youtube_video_id, job.youtube_uploaded_at = video_id, utc_now()
                job.status, job.error_message = JobStatus.SUCCEEDED.value, None
                session.commit()

    def mark_processing(self, job_id: str) -> None:
        SessionLocal = self._session_factory()
        with SessionLocal() as session:
            job = JobRepository(session).get_by_job_id(job_id)
            if job and not job.youtube_video_id:
                job.status = JobStatus.UPLOADING.value
                session.commit()

    async def _already_uploaded(self, job_id: str) -> bool:
        """Persisted video IDs are final: retries only recover notification/cleanup."""
        SessionLocal = self._session_factory()
        with SessionLocal() as session:
            job = JobRepository(session).get_by_job_id(job_id)
            if not job or not job.youtube_video_id:
                return False
            job.status = JobStatus.SUCCEEDED.value
            session.commit()
            try:
                message_id = await notify_youtube_upload_completed(job, f"https://youtu.be/{job.youtube_video_id}")
                if message_id and message_id != job.telegram_message_id:
                    job.telegram_message_id = message_id
                    session.commit()
            except Exception:
                session.rollback()
                logger.warning("Could not acknowledge upload notification for job %s", job_id)
            return True

    def _persist_local_recording_path(self, job_id: str, canonical: CanonicalRecording) -> None:
        """Persist the canonical local recording path after MP4 preparation."""
        SessionLocal = self._session_factory()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            job = repo.get_by_job_id(job_id)
            fields = {"output_path": str(canonical.output_path), "file_size": canonical.file_size}
            if (
                job
                and job.trimmed_output_path
                and Path(job.trimmed_output_path) in recording_file_variants(canonical.output_path)
            ):
                fields["trimmed_output_path"] = str(canonical.output_path)
            if (
                job
                and job.raw_output_path
                and not preserve_raw_recording(job.runtime_summary_json)
                and Path(job.raw_output_path) in recording_file_variants(canonical.output_path)
            ):
                fields["raw_output_path"] = str(canonical.output_path)
            if job:
                for name, value in fields.items():
                    setattr(job, name, value)
            session.commit()
        finally:
            session.close()

    def _mark_upload_completed_without_video(self, job_id: str, message: str) -> None:
        """Return a recording-successful job from upload processing to a terminal state."""
        SessionLocal = self._session_factory()
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
        deleted, errors = delete_recording_artifacts(
            [cleanup_path, prepared_upload_path],
            preserve_path=raw_video_path,
        )
        for candidate in deleted:
            logger.info("Deleted uploaded trimmed artifact: %s", candidate)
        for candidate, exc in errors:
            logger.warning("Failed to delete uploaded trimmed artifact %s: %s", candidate, exc)

        if raw_video_path and raw_video_path.exists():
            SessionLocal = self._session_factory()
            session = SessionLocal()
            try:
                repo = JobRepository(session)
                repo.update_status(job_id, JobStatus.SUCCEEDED.value, output_path=str(raw_video_path))
                session.commit()
            finally:
                session.close()
