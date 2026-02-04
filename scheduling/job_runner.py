import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from config.settings import get_settings
from database.models import (
    JobStatus,
    Schedule,
    get_session_local,
)
from database.session import JobRepository, build_result_update_fields
from recording.remux import ensure_mp4
from recording.worker import RecordingJob, get_worker
from services.app_settings import get_setting_int
from telegram_bot.notifications import (
    notify_recording_completed,
    notify_recording_failed,
    notify_recording_retry,
    notify_recording_started,
    notify_youtube_upload_completed,
)
from uploading.progress import clear_progress, update_progress
from uploading.youtube import (
    UploadStatus,
    VideoMetadata,
    get_youtube_uploader,
)
from utils.timezone import ensure_utc, to_local, utc_now

logger = logging.getLogger(__name__)

# Retry configuration for network errors
INITIAL_RETRY_DELAY_SEC = 15  # Initial delay before first retry
MAX_RETRY_DELAY_SEC = 300  # Maximum delay between retries (5 minutes)
RETRYABLE_ERRORS = [
    "ERR_NAME_NOT_RESOLVED",
    "Name or service not known",
    "No address associated with hostname",
    "ConnectError",
    "TimeoutError",
    "net::ERR_",
    "NetworkError",
]


@dataclass(frozen=True)
class UploadRequest:
    job_id: str
    video_path: Path
    title: str
    privacy: str
    meeting_name: str | None = None


class JobRunner:
    """Job runner with single concurrency enforcement.

    Ensures only one recording job runs at a time by using an asyncio lock.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._upload_lock = asyncio.Lock()
        self._current_schedule_id: int | None = None
        self._queue: list[int] = []  # Queue of schedule IDs waiting to run

    @property
    def is_busy(self) -> bool:
        """Check if a job is currently running."""
        return self._lock.locked()

    @property
    def current_schedule_id(self) -> int | None:
        """Get the currently running schedule ID."""
        return self._current_schedule_id

    @property
    def queue_length(self) -> int:
        """Get number of jobs waiting in queue."""
        return len(self._queue)

    def queue_schedule(self, schedule_id: int) -> bool:
        """Queue a schedule to run.

        If not busy, starts immediately. Otherwise, adds to queue.

        Args:
            schedule_id: Schedule ID to queue

        Returns:
            True if queued/started successfully
        """
        if schedule_id in self._queue:
            logger.warning(f"Schedule {schedule_id} already in queue")
            return False

        # Start async task to run the schedule
        asyncio.create_task(self._run_when_available(schedule_id))
        return True

    async def _run_when_available(self, schedule_id: int) -> None:
        """Wait for lock and run schedule.

        Args:
            schedule_id: Schedule ID to run
        """
        if self._lock.locked():
            logger.info(f"Schedule {schedule_id} waiting in queue (queue length: {len(self._queue)})")
            self._queue.append(schedule_id)

        upload_request = None

        async with self._lock:
            # Remove from queue if present
            if schedule_id in self._queue:
                self._queue.remove(schedule_id)

            self._current_schedule_id = schedule_id
            try:
                upload_request = await self._execute_schedule(schedule_id)
            finally:
                self._current_schedule_id = None

        if upload_request:
            asyncio.create_task(self._run_upload_task(upload_request))

    async def _execute_schedule(self, schedule_id: int) -> UploadRequest | None:
        """Execute a scheduled recording with retry support for network errors.

        Args:
            schedule_id: Schedule ID to execute
        """
        logger.info(f"Executing schedule {schedule_id}")

        SessionLocal = get_session_local()
        session = SessionLocal()

        try:
            # Load schedule with meeting
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()

            if not schedule:
                logger.error(f"Schedule {schedule_id} not found")
                return

            if not schedule.enabled:
                logger.warning(f"Schedule {schedule_id} is disabled, skipping")
                return

            meeting = schedule.meeting
            if not meeting:
                logger.error(f"Meeting not found for schedule {schedule_id}")
                return

            deadline_at = self._get_fixed_deadline_at(schedule)
            if deadline_at:
                logger.info(f"Fixed duration deadline for schedule {schedule_id}: {deadline_at.isoformat()}")

            # Calculate meeting end time for retry deadline
            meeting_end_time = deadline_at or (utc_now() + timedelta(seconds=schedule.duration_sec))

            # Create recording job
            job = RecordingJob.create(
                provider=meeting.provider,
                meeting_code=schedule.get_effective_meeting_code(),
                display_name=schedule.get_effective_display_name(),
                duration_sec=schedule.duration_sec,
                base_url=meeting.site_base_url,
                password=meeting.password_encrypted,
                lobby_wait_sec=get_setting_int(session, "lobby_wait_sec"),
                duration_mode=schedule.duration_mode,
                dry_run=schedule.dry_run,
                min_duration_sec=schedule.min_duration_sec,
                stillness_timeout_sec=schedule.stillness_timeout_sec,
                deadline_at=deadline_at,
            )

            # Store job in database
            repo = JobRepository(session)
            repo.create(
                job_id=job.job_id,
                schedule_id=schedule_id,
                provider=meeting.provider,
                meeting_code=schedule.get_effective_meeting_code(),
                display_name=schedule.get_effective_display_name(),
                base_url=meeting.site_base_url,
                duration_sec=schedule.duration_sec,
                lobby_wait_sec=get_setting_int(session, "lobby_wait_sec"),
                status=JobStatus.QUEUED.value,
            )
            session.commit()

            logger.info(f"Created job {job.job_id} for schedule {schedule_id}")

            # Store schedule info for retry
            youtube_enabled = schedule.youtube_enabled
            youtube_privacy = schedule.youtube_privacy
            meeting_name = meeting.name

        except Exception as e:
            logger.error(f"Failed to create job for schedule {schedule_id}: {e}")
            session.rollback()
            return
        finally:
            session.close()

        # Run the recording with retry logic
        return await self._run_recording_with_retry(
            job=job,
            schedule_id=schedule_id,
            meeting_end_time=meeting_end_time,
            youtube_enabled=youtube_enabled,
            youtube_privacy=youtube_privacy,
            meeting_name=meeting_name,
        )

    def _is_retryable_error(self, error_message: str) -> bool:
        """Check if an error message indicates a retryable network error."""
        error_str = str(error_message)
        return any(pattern in error_str for pattern in RETRYABLE_ERRORS)

    def _get_fixed_deadline_at(self, schedule: Schedule) -> datetime | None:
        """Calculate the fixed deadline for a schedule.

        For CRON schedules, uses croniter to find the most recent scheduled
        fire time, ensuring catch-up runs use the correct window's deadline.
        """
        duration_mode = (
            schedule.duration_mode.value if hasattr(schedule.duration_mode, "value") else schedule.duration_mode
        )
        if duration_mode != "fixed":
            return None

        start_time = None

        # For CRON schedules, use croniter to find the correct window start time
        # This ensures catch-up runs use today's scheduled time, not old last_run_at
        schedule_type_value = (
            schedule.schedule_type.value if hasattr(schedule.schedule_type, "value") else schedule.schedule_type
        )
        if schedule_type_value == "cron" and schedule.cron_expression:
            try:
                settings = get_settings()
                try:
                    tz = ZoneInfo(settings.timezone)
                except Exception:
                    tz = ZoneInfo("UTC")

                now_local = datetime.now(tz)
                cron_iter = croniter(schedule.cron_expression, now_local)
                # Get the most recent fire time (the one that would have triggered this run)
                last_fire = cron_iter.get_prev(datetime)
                if last_fire.tzinfo is None:
                    last_fire = last_fire.replace(tzinfo=tz)
                start_time = ensure_utc(last_fire)
            except Exception as e:
                logger.warning(f"Failed to calculate CRON window start time: {e}")
                # Fall back to existing logic

        # Fall back to existing logic for non-CRON or if croniter failed
        if start_time is None:
            if schedule.start_time:
                start_time = ensure_utc(schedule.start_time)
            elif schedule.last_run_at:
                start_time = ensure_utc(schedule.last_run_at)
            elif schedule.next_run_at:
                start_time = ensure_utc(schedule.next_run_at)
            else:
                start_time = utc_now()

        return start_time + timedelta(seconds=schedule.duration_sec)

    async def _run_recording_with_retry(
        self,
        job: RecordingJob,
        schedule_id: int,
        meeting_end_time,
        youtube_enabled: bool,
        youtube_privacy: str,
        meeting_name: str | None,
    ) -> UploadRequest | None:
        """Run recording with exponential backoff retry for network errors.

        Args:
            job: Recording job to run
            schedule_id: Schedule ID
            meeting_end_time: Deadline for retries (meeting scheduled end time)
            youtube_enabled: Whether YouTube upload is enabled
            youtube_privacy: YouTube privacy setting
        """
        SessionLocal = get_session_local()
        worker = get_worker()
        retry_delay = INITIAL_RETRY_DELAY_SEC
        attempt = 0
        upload_request: UploadRequest | None = None

        def on_status_change(job_id: str, status: JobStatus):
            """Update status in database and send notifications."""
            s = SessionLocal()
            try:
                r = JobRepository(s)
                update_fields = {}
                if status == JobStatus.STARTING:
                    update_fields["started_at"] = utc_now()
                elif status == JobStatus.RECORDING:
                    update_fields["recording_started_at"] = utc_now()
                r.update_status(job_id, status.value, **update_fields)
                s.commit()

                # Send start notification when recording begins and save message_id
                if status == JobStatus.RECORDING:
                    db_job = r.get_by_job_id(job_id)
                    if db_job:

                        async def send_and_save():
                            msg_id = await notify_recording_started(db_job)
                            if msg_id:
                                ss = SessionLocal()
                                try:
                                    rr = JobRepository(ss)
                                    rr.update_status(job_id, status.value, telegram_message_id=msg_id)
                                    ss.commit()
                                finally:
                                    ss.close()

                        asyncio.create_task(send_and_save())
            finally:
                s.close()

        worker.set_status_callback(on_status_change)

        while True:
            attempt += 1
            try:
                result = await worker.record(job)

                # Update database with result
                session = SessionLocal()
                output_path = None

                try:
                    repo = JobRepository(session)
                    update_fields = build_result_update_fields(result)

                    if result.recording_info:
                        output_path = result.recording_info.output_path

                    update_fields["youtube_enabled"] = youtube_enabled
                    repo.update_status(job.job_id, result.status.value, **update_fields)
                    session.commit()

                    # Send completion/failure notification
                    db_job = repo.get_by_job_id(job.job_id)
                    if db_job:
                        if result.status == JobStatus.SUCCEEDED:
                            await notify_recording_completed(db_job)
                        elif result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                            # Check if this is a retryable error
                            error_msg = result.error_message or ""
                            if self._is_retryable_error(error_msg) and utc_now() < meeting_end_time:
                                # Calculate time remaining until meeting end
                                time_remaining = (meeting_end_time - utc_now()).total_seconds()
                                if time_remaining > retry_delay:
                                    logger.warning(
                                        f"Retryable network error for job {job.job_id}: {error_msg}. "
                                        f"Retrying in {retry_delay}s (attempt {attempt})"
                                    )
                                    # Send retry notification
                                    await notify_recording_retry(db_job, attempt, retry_delay, error_msg)

                                    # Wait before retry
                                    await asyncio.sleep(retry_delay)

                                    # Exponential backoff (double the delay, up to max)
                                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY_SEC)

                                    # Recreate job for retry
                                    job = RecordingJob.create(
                                        provider=job.provider,
                                        meeting_code=job.meeting_code,
                                        display_name=job.display_name,
                                        duration_sec=int(time_remaining),  # Remaining time
                                        base_url=job.base_url,
                                        lobby_wait_sec=job.lobby_wait_sec,
                                        duration_mode=job.duration_mode,
                                        dry_run=job.dry_run,
                                        min_duration_sec=job.min_duration_sec,
                                        stillness_timeout_sec=job.stillness_timeout_sec,
                                        deadline_at=job.deadline_at,
                                    )
                                    session.close()
                                    continue  # Retry the recording
                                else:
                                    logger.warning(
                                        f"Not enough time remaining for retry (need {retry_delay}s, have {time_remaining}s)"
                                    )
                            # Not retryable or no time left - send failure notification
                            await notify_recording_failed(db_job)

                    logger.info(f"Job {job.job_id} completed with status: {result.status.value}")
                finally:
                    session.close()

                # YouTube upload if enabled and recording succeeded
                if youtube_enabled and result.status == JobStatus.SUCCEEDED and output_path and output_path.exists():
                    # Build title with recording start time
                    recording_time = result.recording_started_at or result.start_time or utc_now()
                    # Convert to local time for user-friendly title
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
                    )

                # Recording completed (success or non-retryable failure)
                break

            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error running job for schedule {schedule_id}: {error_msg}")

                # Check if this exception is retryable
                if self._is_retryable_error(error_msg) and utc_now() < meeting_end_time:
                    time_remaining = (meeting_end_time - utc_now()).total_seconds()
                    if time_remaining > retry_delay:
                        logger.warning(
                            f"Retryable exception for schedule {schedule_id}: {error_msg}. "
                            f"Retrying in {retry_delay}s (attempt {attempt})"
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY_SEC)
                        continue  # Retry
                # Not retryable or no time left
                break

        return upload_request

    async def _run_upload_task(self, upload_request: UploadRequest) -> None:
        upload_job_id = upload_request.job_id
        try:
            video_path = upload_request.video_path
            remux_log = None
            transcode_log = None
            if video_path.suffix.lower() == ".mkv":
                settings = get_settings()
                remux_log = settings.diagnostics_dir / upload_request.job_id / "remux.log"
                transcode_log = settings.diagnostics_dir / upload_request.job_id / "transcode.log"

            def transcode_progress(current_ms: int | None, total_ms: int | None):
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
                await self._upload_to_youtube(
                    job_id=upload_request.job_id,
                    video_path=upload_path,
                    title=upload_request.title,
                    privacy=upload_request.privacy,
                )
        except Exception as e:
            logger.error(f"YouTube upload task failed for job {upload_request.job_id}: {e}")
        finally:
            clear_progress(upload_job_id)

    async def _upload_to_youtube(
        self,
        job_id: str,
        video_path: Path,
        title: str,
        privacy: str = "unlisted",
    ) -> None:
        """Upload recording to YouTube.

        Args:
            job_id: Job ID for status updates
            video_path: Path to video file
            title: Video title
            privacy: Privacy status (public, private, unlisted)
        """
        logger.info(f"Starting YouTube upload for job {job_id}")
        try:
            file_size = video_path.stat().st_size
        except OSError:
            file_size = None
        update_progress(job_id, "uploading", 0, file_size, "bytes")

        SessionLocal = get_session_local()
        session = SessionLocal()

        try:
            # Update status to uploading
            repo = JobRepository(session)
            repo.update_status(job_id, JobStatus.UPLOADING.value)
            session.commit()
        finally:
            session.close()

        # Get uploader
        uploader = get_youtube_uploader()

        if not uploader.is_configured:
            logger.warning("YouTube upload skipped - not configured")
            return

        if not uploader.is_authorized:
            logger.warning("YouTube upload skipped - not authorized")
            return

        try:
            # Prepare metadata
            metadata = VideoMetadata(
                title=title,
                description=f"Recorded meeting - {job_id}",
                privacy_status=privacy,
            )

            # Upload with progress logging
            def log_progress(uploaded: int, total: int):
                percent = (uploaded / total * 100) if total > 0 else 0
                logger.debug(f"Upload progress: {percent:.1f}% ({uploaded}/{total} bytes)")
                update_progress(job_id, "uploading", uploaded, total, "bytes")

            result = await uploader.upload_video(
                video_path=video_path,
                metadata=metadata,
                progress_callback=log_progress,
            )

            # Update database with result
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

                    # Send YouTube upload notification
                    db_job = repo.get_by_job_id(job_id)
                    if db_job and result.video_url:
                        await notify_youtube_upload_completed(db_job, result.video_url)
                else:
                    # Upload failed but recording succeeded - keep succeeded status
                    logger.error(f"YouTube upload failed: {result.error_message}")
                    session.commit()
            finally:
                session.close()

        except Exception as e:
            logger.error(f"YouTube upload error: {e}")

    async def run_immediate(
        self,
        provider: str,
        meeting_code: str,
        display_name: str,
        duration_sec: int,
        base_url: str | None = None,
        lobby_wait_sec: int = 900,
    ) -> str | None:
        """Run a recording immediately (not from schedule).

        Args:
            provider: Meeting provider
            meeting_code: Meeting code
            display_name: Display name
            duration_sec: Duration in seconds
            base_url: Base URL override
            lobby_wait_sec: Lobby wait time

        Returns:
            Job ID if started, None if busy
        """
        if self._lock.locked():
            logger.warning("Cannot run immediate job - worker is busy")
            return None

        job = RecordingJob.create(
            provider=provider,
            meeting_code=meeting_code,
            display_name=display_name,
            duration_sec=duration_sec,
            base_url=base_url,
            lobby_wait_sec=lobby_wait_sec,
        )

        # Create task to run the job
        asyncio.create_task(self._run_immediate_job(job))
        return job.job_id

    async def _run_immediate_job(self, job: RecordingJob) -> None:
        """Run an immediate job with lock.

        Args:
            job: RecordingJob to run
        """
        async with self._lock:
            SessionLocal = get_session_local()
            session = SessionLocal()

            try:
                # Store job in database
                repo = JobRepository(session)
                repo.create(
                    job_id=job.job_id,
                    provider=job.provider,
                    meeting_code=job.meeting_code,
                    display_name=job.display_name,
                    base_url=job.base_url,
                    duration_sec=job.duration_sec,
                    lobby_wait_sec=job.lobby_wait_sec,
                    status=JobStatus.QUEUED.value,
                )
                session.commit()
            finally:
                session.close()

            # Set up status callback for start notification
            def on_status_change(job_id: str, status: JobStatus):
                """Update status in database and send notifications."""
                s = SessionLocal()
                try:
                    r = JobRepository(s)
                    update_fields = {}
                    if status == JobStatus.STARTING:
                        update_fields["started_at"] = utc_now()
                    elif status == JobStatus.RECORDING:
                        update_fields["recording_started_at"] = utc_now()
                    r.update_status(job_id, status.value, **update_fields)
                    s.commit()

                    # Send start notification when recording begins
                    if status == JobStatus.RECORDING:
                        db_job = r.get_by_job_id(job_id)
                        if db_job:
                            asyncio.create_task(notify_recording_started(db_job))
                finally:
                    s.close()

            # Run recording
            worker = get_worker()
            worker.set_status_callback(on_status_change)
            result = await worker.record(job)

            # Update database
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


# Global job runner instance
_runner_instance: JobRunner | None = None


def get_job_runner() -> JobRunner:
    """Get the global job runner instance."""
    global _runner_instance
    if _runner_instance is None:
        _runner_instance = JobRunner()
    return _runner_instance
