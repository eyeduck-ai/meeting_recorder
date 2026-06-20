import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from config.settings import get_settings
from database.models import JobStatus, Schedule
from database.session import JobRepository, get_session_local
from recording.worker import RecordingJob, get_worker
from scheduling.recording_executor import RecordingExecutor
from scheduling.schedule_queue import QueueScheduleResult, ScheduleRunQueue
from scheduling.upload_runner import UploadRequest, YouTubeUploadRunner
from services.runtime_config import get_runtime_config_service
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)


def calculate_retry_window_end(base_end_time: datetime, runtime_config) -> datetime:
    """Return the bounded retry window end for a recording run."""
    if runtime_config.dynamic_extension_enabled and runtime_config.dynamic_extension_max_sec > 0:
        return base_end_time + timedelta(seconds=runtime_config.dynamic_extension_max_sec)
    return base_end_time


class JobRunner:
    """Job runner with single concurrency enforcement."""

    def __init__(self, *, worker=None):
        self._worker = worker
        self._lock = asyncio.Lock()
        self._recording_executor = RecordingExecutor(worker_provider=self._get_worker)
        self._upload_runner = YouTubeUploadRunner()
        self._schedule_queue = ScheduleRunQueue()
        self._queue_processor_task: asyncio.Task | None = None

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def current_schedule_id(self) -> int | None:
        return self._schedule_queue.current_schedule_id

    @property
    def queue_length(self) -> int:
        return self._schedule_queue.queue_length

    def queue_schedule(self, schedule_id: int, manual_trigger: bool = False) -> QueueScheduleResult:
        """Queue a schedule to run."""
        result = self._schedule_queue.enqueue(
            schedule_id,
            manual_trigger=manual_trigger,
            lock_busy=self._lock.locked(),
        )
        if not result.accepted:
            logger.warning(f"Schedule {schedule_id} is already running or queued")
            return result

        if result.status == "queued":
            logger.info(f"Schedule {schedule_id} waiting in queue (queue position: {result.queue_position})")

        self._ensure_queue_processor()
        return result

    def _ensure_queue_processor(self) -> None:
        """Start the schedule queue processor if no active processor exists."""
        if self._queue_processor_task and not self._queue_processor_task.done():
            return
        self._queue_processor_task = asyncio.create_task(self._process_schedule_queue())

    async def _process_schedule_queue(self) -> None:
        """Process queued schedules in FIFO order."""
        try:
            while self._schedule_queue.has_queued:
                upload_request = None
                async with self._lock:
                    queue_item = self._schedule_queue.pop_next()
                    if not queue_item:
                        continue
                    try:
                        upload_request = await self._execute_schedule(
                            queue_item.schedule_id,
                            manual_trigger=queue_item.manual_trigger,
                        )
                    finally:
                        self._schedule_queue.mark_current_done()

                if upload_request:
                    asyncio.create_task(self._run_upload_task(upload_request))
        finally:
            self._queue_processor_task = None
            if self._schedule_queue.has_queued:
                self._ensure_queue_processor()

    def _mark_schedule_started(self, schedule_id: int) -> None:
        """Mark the schedule as actually starting a job."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
            if schedule:
                started_at = utc_now()
                schedule.last_started_at = started_at
                schedule.last_run_at = started_at
                session.commit()
        finally:
            session.close()

    def _mark_schedule_completed(self, schedule_id: int) -> None:
        """Mark the schedule's latest job as completed."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
            if schedule:
                schedule.last_completed_at = utc_now()
                session.commit()
        finally:
            session.close()

    def is_schedule_active_or_queued(self, schedule_id: int) -> bool:
        """Return whether a schedule is currently running or waiting in queue."""
        return self._schedule_queue.is_schedule_active_or_queued(schedule_id)

    async def _execute_schedule(self, schedule_id: int, *, manual_trigger: bool = False) -> UploadRequest | None:
        """Execute a scheduled recording."""
        logger.info(f"Executing schedule {schedule_id}")
        SessionLocal = get_session_local()
        session = SessionLocal()
        schedule_started = False

        try:
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
            if not schedule:
                logger.error(f"Schedule {schedule_id} not found")
                return None
            if not schedule.enabled:
                logger.warning(f"Schedule {schedule_id} is disabled, skipping")
                return None
            meeting = schedule.meeting
            if not meeting:
                logger.error(f"Meeting not found for schedule {schedule_id}")
                return None

            self._mark_schedule_started(schedule_id)
            schedule_started = True

            deadline_at = self._get_fixed_deadline_at(schedule, manual_trigger=manual_trigger)
            if deadline_at:
                logger.info(f"Fixed duration deadline for schedule {schedule_id}: {deadline_at.isoformat()}")
            runtime_config = get_runtime_config_service().get_recording_config(
                session,
                lobby_wait_sec=schedule.lobby_wait_sec,
                resolution_w=schedule.resolution_w,
                resolution_h=schedule.resolution_h,
                smart_trim_enabled=schedule.smart_trim_enabled,
                dynamic_extension_enabled=schedule.dynamic_extension_enabled,
                dynamic_extension_idle_sec=schedule.dynamic_extension_idle_sec,
                dynamic_extension_max_sec=schedule.dynamic_extension_max_sec,
            )
            base_end_time = deadline_at or (utc_now() + timedelta(seconds=schedule.duration_sec))
            meeting_end_time = calculate_retry_window_end(base_end_time, runtime_config)

            job = RecordingJob.create(
                provider=meeting.provider,
                meeting_code=schedule.get_effective_meeting_code(),
                display_name=schedule.get_effective_display_name(),
                duration_sec=schedule.duration_sec,
                base_url=meeting.site_base_url,
                password=meeting.meeting_password_plaintext,
                runtime_config=runtime_config,
                deadline_at=deadline_at,
            )
            self._persist_job_created(
                session=session,
                job=job,
                schedule_id=schedule_id,
                provider=meeting.provider,
                meeting_code=schedule.get_effective_meeting_code(),
                display_name=schedule.get_effective_display_name(),
                base_url=meeting.site_base_url,
                duration_sec=schedule.duration_sec,
                lobby_wait_sec=job.lobby_wait_sec,
            )
            session.commit()
            logger.info(f"Created job {job.job_id} for schedule {schedule_id}")

            return await self._run_recording_with_retry(
                job=job,
                schedule_id=schedule_id,
                meeting_end_time=meeting_end_time,
                youtube_enabled=schedule.youtube_enabled,
                youtube_privacy=schedule.youtube_privacy,
                meeting_name=meeting.name,
            )
        except Exception as e:
            logger.error(f"Failed to create job for schedule {schedule_id}: {e}")
            session.rollback()
            return None
        finally:
            session.close()
            if schedule_started:
                self._mark_schedule_completed(schedule_id)

    async def run_immediate(
        self,
        provider: str,
        meeting_code: str,
        display_name: str,
        duration_sec: int,
        base_url: str | None = None,
        password: str | None = None,
        lobby_wait_sec: int | None = None,
    ) -> str | None:
        """Run a recording immediately using the same execution path as schedules."""
        if self._lock.locked():
            logger.warning("Cannot run immediate job - worker is busy")
            return None

        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            runtime_config = get_runtime_config_service().get_recording_config(
                session,
                lobby_wait_sec=lobby_wait_sec,
            )
            job = RecordingJob.create(
                provider=provider,
                meeting_code=meeting_code,
                display_name=display_name,
                duration_sec=duration_sec,
                base_url=base_url,
                password=password,
                runtime_config=runtime_config,
            )
            self._persist_job_created(
                session=session,
                job=job,
                schedule_id=None,
                provider=provider,
                meeting_code=meeting_code,
                display_name=display_name,
                base_url=base_url,
                duration_sec=duration_sec,
                lobby_wait_sec=job.lobby_wait_sec,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        base_end_time = utc_now() + timedelta(seconds=duration_sec)
        retry_window_end = calculate_retry_window_end(base_end_time, runtime_config)

        asyncio.create_task(
            self._run_direct_job(
                job=job,
                meeting_end_time=retry_window_end,
                youtube_enabled=False,
                youtube_privacy="unlisted",
                meeting_name=None,
            )
        )
        return job.job_id

    async def _run_direct_job(
        self,
        *,
        job: RecordingJob,
        meeting_end_time: datetime,
        youtube_enabled: bool,
        youtube_privacy: str,
        meeting_name: str | None,
    ) -> None:
        """Run an already-created job under the global worker lock."""
        upload_request = None
        async with self._lock:
            upload_request = await self._run_recording_with_retry(
                job=job,
                schedule_id=None,
                meeting_end_time=meeting_end_time,
                youtube_enabled=youtube_enabled,
                youtube_privacy=youtube_privacy,
                meeting_name=meeting_name,
            )

        if upload_request:
            asyncio.create_task(self._run_upload_task(upload_request))

    def _persist_job_created(
        self,
        *,
        session,
        job: RecordingJob,
        schedule_id: int | None,
        provider: str,
        meeting_code: str,
        display_name: str,
        base_url: str | None,
        duration_sec: int,
        lobby_wait_sec: int,
    ) -> None:
        """Persist the initial DB row for a new recording job."""
        repo = JobRepository(session)
        repo.create(
            job_id=job.job_id,
            schedule_id=schedule_id,
            provider=provider,
            meeting_code=meeting_code,
            display_name=display_name,
            base_url=base_url,
            duration_sec=duration_sec,
            lobby_wait_sec=lobby_wait_sec,
            status=JobStatus.QUEUED.value,
            attempt_no=job.attempt_no,
            retry_count=max(0, job.attempt_no - 1),
        )

    def _get_fixed_deadline_at(self, schedule: Schedule, *, manual_trigger: bool = False) -> datetime | None:
        """Calculate the fixed deadline for a schedule."""
        if manual_trigger:
            return utc_now() + timedelta(seconds=schedule.duration_sec)

        start_time = None
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
                last_fire = cron_iter.get_prev(datetime)
                if last_fire.tzinfo is None:
                    last_fire = last_fire.replace(tzinfo=tz)
                start_time = ensure_utc(last_fire)
            except Exception as e:
                logger.warning(f"Failed to calculate CRON window start time: {e}")

        if start_time is None:
            if schedule.start_time:
                start_time = ensure_utc(schedule.start_time)
            elif schedule.last_started_at:
                start_time = ensure_utc(schedule.last_started_at)
            elif schedule.last_run_at:
                start_time = ensure_utc(schedule.last_run_at)
            elif schedule.next_run_at:
                start_time = ensure_utc(schedule.next_run_at)
            else:
                start_time = utc_now()

        return start_time + timedelta(seconds=schedule.duration_sec)

    async def _run_recording_with_retry(
        self,
        *,
        job: RecordingJob,
        schedule_id: int | None,
        meeting_end_time: datetime,
        youtube_enabled: bool,
        youtube_privacy: str,
        meeting_name: str | None,
    ) -> UploadRequest | None:
        """Run recording with retry using the recording executor."""
        return await self._recording_executor.run_with_retry(
            job=job,
            schedule_id=schedule_id,
            meeting_end_time=meeting_end_time,
            youtube_enabled=youtube_enabled,
            youtube_privacy=youtube_privacy,
            meeting_name=meeting_name,
        )

    def _get_worker(self):
        return self._worker or get_worker()

    async def _run_upload_task(self, upload_request: UploadRequest) -> None:
        await self._upload_runner.run_upload_task(upload_request)


_runner_instance: JobRunner | None = None


def get_job_runner() -> JobRunner:
    """Get the global job runner instance."""
    global _runner_instance
    if _runner_instance is None:
        _runner_instance = JobRunner()
    return _runner_instance


def set_job_runner_instance(runner: JobRunner) -> None:
    """Set the compatibility job runner singleton."""
    global _runner_instance
    _runner_instance = runner


def reset_job_runner_instance() -> None:
    """Clear the compatibility job runner singleton."""
    global _runner_instance
    _runner_instance = None
