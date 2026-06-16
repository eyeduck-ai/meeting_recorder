"""Recording execution, retry, DB status, and notification flow."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from database.models import JobStatus
from database.session import JobRepository, build_result_update_fields, get_session_local
from recording.worker import RecordingJob
from scheduling.upload_runner import UploadRequest
from services.storage_maintenance import canonicalize_recording_file
from telegram_bot.notifications import (
    notify_recording_completed,
    notify_recording_failed,
    notify_recording_retry,
    notify_recording_status,
)
from utils.timezone import to_local, utc_now

logger = logging.getLogger(__name__)

INITIAL_RETRY_DELAY_SEC = 15
MAX_RETRY_DELAY_SEC = 300
RETRYABLE_ERRORS = [
    "ERR_NAME_NOT_RESOLVED",
    "Name or service not known",
    "No address associated with hostname",
    "ConnectError",
    "TimeoutError",
    "net::ERR_",
    "NetworkError",
]

ACTIVE_NOTIFICATION_STATUSES = {
    JobStatus.STARTING,
    JobStatus.JOINING,
    JobStatus.WAITING_LOBBY,
    JobStatus.RECORDING,
    JobStatus.FINALIZING,
}


class RecordingExecutor:
    """Run a recording job with retry, status persistence, and notifications."""

    def __init__(self, *, worker_provider: Callable[[], object]):
        self._worker_provider = worker_provider
        self._notification_lock = asyncio.Lock()

    async def run_with_retry(
        self,
        *,
        job: RecordingJob,
        schedule_id: int | None,
        meeting_end_time: datetime,
        youtube_enabled: bool,
        youtube_privacy: str,
        meeting_name: str | None,
    ) -> UploadRequest | None:
        """Run recording with exponential backoff retry for network errors."""
        SessionLocal = get_session_local()
        worker = self._worker_provider()
        retry_delay = INITIAL_RETRY_DELAY_SEC
        attempt = 0
        upload_request: UploadRequest | None = None

        def on_status_change(job_id: str, status: JobStatus) -> None:
            s = SessionLocal()
            try:
                repo = JobRepository(s)
                update_fields = {
                    "attempt_no": job.attempt_no,
                    "retry_count": max(0, job.attempt_no - 1),
                }
                if status == JobStatus.STARTING:
                    update_fields["started_at"] = utc_now()
                elif status == JobStatus.RECORDING:
                    update_fields["recording_started_at"] = utc_now()
                repo.update_status(job_id, status.value, **update_fields)
                s.commit()

                if status in ACTIVE_NOTIFICATION_STATUSES:
                    asyncio.create_task(self._notify_stage_update(job_id, status))
            finally:
                s.close()

        worker.set_status_callback(on_status_change)

        while True:
            attempt += 1
            job.attempt_no = attempt
            self._mark_retry_attempt(job)
            result = await worker.record(job)
            if result.status == JobStatus.SUCCEEDED and result.recording_info:
                await self._canonicalize_successful_recording(job, result)

            session = SessionLocal()
            output_path = None
            try:
                repo = JobRepository(session)
                update_fields = build_result_update_fields(result)
                update_fields["attempt_no"] = attempt
                update_fields["retry_count"] = max(0, attempt - 1)
                update_fields["youtube_enabled"] = youtube_enabled

                if result.recording_info:
                    output_path = getattr(result, "output_path", None) or result.recording_info.output_path

                repo.update_status(job.job_id, result.status.value, **update_fields)
                session.commit()

                db_job = repo.get_by_job_id(job.job_id)
                should_retry = False
                error_msg = result.error_message or ""
                if result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                    should_retry = self._is_retryable_error(error_msg) and utc_now() < meeting_end_time

                if db_job and should_retry:
                    time_remaining = (meeting_end_time - utc_now()).total_seconds()
                    if time_remaining > retry_delay:
                        logger.warning(
                            f"Retryable network error for job {job.job_id}: {error_msg}. "
                            f"Retrying in {retry_delay}s (attempt {attempt})"
                        )
                        await notify_recording_retry(db_job, attempt, retry_delay, error_msg)
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY_SEC)
                        job.duration_sec = int(time_remaining)
                        continue

                if db_job:
                    if result.status == JobStatus.SUCCEEDED:
                        await notify_recording_completed(db_job)
                    elif result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                        await notify_recording_failed(db_job)

                logger.info(f"Job {job.job_id} completed with status: {result.status.value}")
            finally:
                session.close()

            if youtube_enabled and result.status == JobStatus.SUCCEEDED and output_path and output_path.exists():
                raw_output_path = getattr(result, "raw_output_path", None)
                trimmed_output_path = getattr(result, "trimmed_output_path", None)
                cleanup_path = None
                if trimmed_output_path and output_path == trimmed_output_path:
                    cleanup_path = trimmed_output_path
                recording_time = result.recording_started_at or result.start_time or utc_now()
                local_time = to_local(recording_time)
                time_str = local_time.strftime("%Y%m%d_%H%M")
                title_parts = [time_str]
                if meeting_name:
                    title_parts.append(meeting_name)
                title_parts.append(job.meeting_code)
                upload_request = UploadRequest(
                    job_id=job.job_id,
                    video_path=output_path,
                    title=" - ".join(title_parts),
                    privacy=youtube_privacy,
                    meeting_name=meeting_name,
                    raw_video_path=raw_output_path,
                    cleanup_video_path_after_success=cleanup_path,
                )
            break

        return upload_request

    def _is_retryable_error(self, error_message: str) -> bool:
        error_str = str(error_message)
        return any(pattern in error_str for pattern in RETRYABLE_ERRORS)

    async def _canonicalize_successful_recording(self, job: RecordingJob, result) -> None:
        """Best-effort conversion to the local MP4 canonical file."""
        recording_info = result.recording_info
        if not recording_info or recording_info.output_path.suffix.lower() != ".mkv":
            return

        original_output_path = recording_info.output_path
        remux_log = job.diagnostics_dir / "remux.log" if job.diagnostics_dir else None
        transcode_log = job.diagnostics_dir / "transcode.log" if job.diagnostics_dir else None
        try:
            canonical = await canonicalize_recording_file(
                original_output_path,
                remux_log_path=remux_log,
                transcode_log_path=transcode_log,
            )
        except Exception as exc:
            logger.warning("Recording canonicalization failed for job %s: %s", job.job_id, exc)
            return

        if not canonical:
            logger.warning("Recording canonicalization did not produce MP4 for job %s", job.job_id)
            return

        result.recording_info = replace(
            recording_info,
            output_path=canonical.output_path,
            file_size=canonical.file_size,
        )
        self._replace_result_path_if_original(result, "output_path", original_output_path, canonical.output_path)
        self._replace_result_path_if_original(
            result, "trimmed_output_path", original_output_path, canonical.output_path
        )
        self._replace_result_path_if_original(result, "raw_output_path", original_output_path, canonical.output_path)
        runtime_summary = getattr(result, "runtime_summary", None)
        if isinstance(runtime_summary, dict) and isinstance(runtime_summary.get("recording_info"), dict):
            runtime_summary["recording_info"]["output_path"] = str(canonical.output_path)
            runtime_summary["recording_info"]["file_size"] = canonical.file_size
        if isinstance(runtime_summary, dict) and isinstance(runtime_summary.get("trim"), dict):
            trim_summary = runtime_summary["trim"]
            for key in ("raw_output_path", "trimmed_output_path"):
                if self._path_value_matches(trim_summary.get(key), original_output_path):
                    trim_summary[key] = str(canonical.output_path)

    @staticmethod
    def _replace_result_path_if_original(result, attr: str, original_path: Path, canonical_path: Path) -> None:
        """Update result path overrides that still point at the pre-canonical file."""
        value = getattr(result, attr, None)
        if RecordingExecutor._path_value_matches(value, original_path):
            setattr(result, attr, canonical_path)

    @staticmethod
    def _path_value_matches(value, expected: Path) -> bool:
        if not isinstance(value, str | Path):
            return False
        return Path(value) == expected

    def _mark_retry_attempt(self, job: RecordingJob) -> None:
        """Persist the current attempt before starting the worker."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            repo.update_status(
                job.job_id,
                JobStatus.QUEUED.value,
                attempt_no=job.attempt_no,
                retry_count=max(0, job.attempt_no - 1),
            )
            session.commit()
        finally:
            session.close()

    async def _notify_stage_update(self, job_id: str, status: JobStatus) -> None:
        """Send stage update notification and persist Telegram message ID."""
        async with self._notification_lock:
            SessionLocal = get_session_local()
            session = SessionLocal()
            try:
                repo = JobRepository(session)
                db_job = repo.get_by_job_id(job_id)
                if not db_job:
                    return

                message_id = await notify_recording_status(db_job, status)
                if message_id and not db_job.telegram_message_id:
                    repo.update_status(job_id, db_job.status, telegram_message_id=message_id)
                    session.commit()
            except Exception as e:
                logger.warning(f"Failed to send stage notification for job {job_id}: {e}")
                session.rollback()
            finally:
                session.close()
