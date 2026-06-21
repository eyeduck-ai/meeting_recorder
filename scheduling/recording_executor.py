"""Recording execution, retry, DB status, and notification flow."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from database.models import JobStatus
from database.session import JobRepository, build_result_update_fields, get_session_local
from recording.job_types import RecordingJob
from recording.post_processing import RecordingPostProcessingRequest
from scheduling.upload_runner import UploadRequest
from telegram_bot.notifications import (
    notify_recording_failed,
    notify_recording_retry,
    notify_recording_status,
)
from utils.timezone import ensure_utc, utc_now

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


@dataclass(frozen=True)
class RecordingRetryRequest:
    """A retryable recording attempt that should be requeued after a delay."""

    job: RecordingJob
    schedule_id: int | None
    meeting_end_time: datetime
    youtube_enabled: bool
    youtube_privacy: str
    meeting_name: str | None
    delay_sec: int
    next_retry_delay_sec: int
    error_message: str


RecordingExecutionOutcome = RecordingPostProcessingRequest | UploadRequest | RecordingRetryRequest | None


class RecordingExecutor:
    """Run a recording job with retry, status persistence, and notifications."""

    def __init__(self, *, worker_provider: Callable[[], object]):
        self._worker_provider = worker_provider
        self._notification_lock = asyncio.Lock()
        self._attempt_by_job_id: dict[str, int] = {}

    async def run_with_retry(
        self,
        *,
        job: RecordingJob,
        schedule_id: int | None,
        meeting_end_time: datetime,
        youtube_enabled: bool,
        youtube_privacy: str,
        meeting_name: str | None,
        retry_delay_sec: int = INITIAL_RETRY_DELAY_SEC,
    ) -> RecordingExecutionOutcome:
        """Run one recording attempt and return upload or delayed retry work."""
        SessionLocal = get_session_local()
        worker = self._worker_provider()
        attempt = max(1, int(getattr(job, "attempt_no", 1) or 1))

        worker.set_status_callback(self._on_status_change)

        try:
            job.attempt_no = attempt
            self._attempt_by_job_id[job.job_id] = attempt
            self._mark_retry_attempt(job)
            result = await worker.record(job)

            session = SessionLocal()
            try:
                repo = JobRepository(session)
                update_fields = build_result_update_fields(result)
                update_fields["attempt_no"] = attempt
                update_fields["retry_count"] = max(0, attempt - 1)
                update_fields["youtube_enabled"] = youtube_enabled

                finalizing_notification_needed = False
                if result.status == JobStatus.SUCCEEDED and result.recording_info:
                    update_fields["completed_at"] = None
                    repo.update_status(job.job_id, JobStatus.FINALIZING.value, **update_fields)
                    finalizing_notification_needed = True
                else:
                    repo.update_status(job.job_id, result.status.value, **update_fields)
                session.commit()
                if finalizing_notification_needed:
                    self._schedule_stage_notification(job.job_id, JobStatus.FINALIZING)

                db_job = repo.get_by_job_id(job.job_id)
                should_retry = False
                error_msg = result.error_message or ""
                meeting_end_time = ensure_utc(meeting_end_time) or meeting_end_time
                if result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                    should_retry = self._is_retryable_error(error_msg) and utc_now() < meeting_end_time

                if db_job and should_retry:
                    time_remaining = (meeting_end_time - utc_now()).total_seconds()
                    if time_remaining > retry_delay_sec:
                        logger.warning(
                            f"Retryable network error for job {job.job_id}: {error_msg}. "
                            f"Requeueing in {retry_delay_sec}s (attempt {attempt})"
                        )
                        await notify_recording_retry(db_job, attempt, retry_delay_sec, error_msg)
                        self._prepare_retry_job(
                            job,
                            meeting_end_time=meeting_end_time,
                            time_remaining_sec=time_remaining,
                            next_attempt=attempt + 1,
                        )
                        repo.update_status(
                            job.job_id,
                            JobStatus.QUEUED.value,
                            attempt_no=attempt,
                            retry_count=max(0, attempt - 1),
                            error_message=f"Retry scheduled after recording failure: {error_msg}",
                            completed_at=None,
                            end_reason=None,
                        )
                        session.commit()
                        return RecordingRetryRequest(
                            job=job,
                            schedule_id=schedule_id,
                            meeting_end_time=meeting_end_time,
                            youtube_enabled=youtube_enabled,
                            youtube_privacy=youtube_privacy,
                            meeting_name=meeting_name,
                            delay_sec=retry_delay_sec,
                            next_retry_delay_sec=min(retry_delay_sec * 2, MAX_RETRY_DELAY_SEC),
                            error_message=error_msg,
                        )

                if db_job:
                    if result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                        await notify_recording_failed(db_job)

                logger.info(f"Job {job.job_id} completed with status: {result.status.value}")
            finally:
                session.close()

            if result.status == JobStatus.SUCCEEDED and result.recording_info:
                return RecordingPostProcessingRequest(
                    job=job,
                    result=result,
                    youtube_enabled=youtube_enabled,
                    youtube_privacy=youtube_privacy,
                    meeting_name=meeting_name,
                )
        finally:
            self._attempt_by_job_id.pop(job.job_id, None)

        return None

    def _on_status_change(self, job_id: str, status: JobStatus) -> None:
        SessionLocal = get_session_local()
        s = SessionLocal()
        try:
            attempt = self._attempt_by_job_id.get(job_id, 1)
            repo = JobRepository(s)
            update_fields = {
                "attempt_no": attempt,
                "retry_count": max(0, attempt - 1),
            }
            if status == JobStatus.STARTING:
                update_fields["started_at"] = utc_now()
            elif status == JobStatus.RECORDING:
                update_fields["recording_started_at"] = utc_now()
            repo.update_status(job_id, status.value, **update_fields)
            s.commit()

            if status in ACTIVE_NOTIFICATION_STATUSES:
                self._schedule_stage_notification(job_id, status)
        finally:
            s.close()

    def _schedule_stage_notification(self, job_id: str, status: JobStatus) -> None:
        asyncio.create_task(self._notify_stage_update(job_id, status))

    def _is_retryable_error(self, error_message: str) -> bool:
        error_str = str(error_message)
        return any(pattern in error_str for pattern in RETRYABLE_ERRORS)

    def _prepare_retry_job(
        self,
        job: RecordingJob,
        *,
        meeting_end_time: datetime,
        time_remaining_sec: float,
        next_attempt: int,
    ) -> None:
        """Prepare the next attempt without counting dynamic extension twice."""
        job.hard_deadline_at = meeting_end_time
        job.duration_sec = self._retry_baseline_duration_sec(job, time_remaining_sec)
        job.attempt_no = next_attempt

    def _retry_baseline_duration_sec(self, job: RecordingJob, time_remaining_sec: float) -> int:
        now = utc_now()
        deadline_at = ensure_utc(getattr(job, "deadline_at", None))
        if deadline_at:
            if deadline_at > now:
                return max(1, int(min(time_remaining_sec, (deadline_at - now).total_seconds())))
            job.deadline_at = None

        dynamic_extension_max = 0
        if bool(getattr(job, "dynamic_extension_enabled", False)):
            dynamic_extension_max = max(0, int(getattr(job, "dynamic_extension_max_sec", 0) or 0))
        if dynamic_extension_max > 0:
            baseline_remaining = time_remaining_sec - dynamic_extension_max
            if baseline_remaining > 0:
                return max(1, int(baseline_remaining))
            return 1

        return max(1, int(time_remaining_sec))

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

                db_status = db_job.status.value if hasattr(db_job.status, "value") else db_job.status
                if db_status != status.value:
                    logger.debug(
                        "Skipping stale stage notification for job %s: requested=%s current=%s",
                        job_id,
                        status.value,
                        db_status,
                    )
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
