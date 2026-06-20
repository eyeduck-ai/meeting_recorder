import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from config.settings import get_settings
from database.models import ErrorCode, JobStatus, Schedule
from database.session import JobRepository, get_session_local
from recording.worker import RecordingJob, get_worker
from scheduling.recording_executor import RecordingExecutionOutcome, RecordingExecutor, RecordingRetryRequest
from scheduling.schedule_queue import QueuedRunItem, QueuedRunView, QueueScheduleResult, ScheduleRunQueue
from scheduling.upload_runner import UploadRequest, YouTubeUploadRunner
from services.runtime_config import get_runtime_config_service
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)


def calculate_retry_window_end(base_end_time: datetime, runtime_config) -> datetime:
    """Return the bounded retry window end for a recording run."""
    if runtime_config.dynamic_extension_enabled and runtime_config.dynamic_extension_max_sec > 0:
        return base_end_time + timedelta(seconds=runtime_config.dynamic_extension_max_sec)
    return base_end_time


@dataclass(frozen=True)
class DirectRecordingQueueItem:
    """A queued immediate recording that already has a persisted DB row."""

    job: RecordingJob
    meeting_end_time: datetime
    youtube_enabled: bool
    youtube_privacy: str
    meeting_name: str | None
    schedule_id: int | None = None
    retry_delay_sec: int | None = None


@dataclass(frozen=True)
class RetryWaitingView:
    """Read-only view of a delayed retry that is not yet in the FIFO queue."""

    job_id: str
    schedule_id: int | None
    status: str
    retry_after_sec: int
    meeting_code: str | None
    display_name: str | None


@dataclass(frozen=True)
class QueuedJobCancelResult:
    """Structured result for canceling queued work from a lifecycle action."""

    removed: bool
    source: Literal["fifo", "retry_waiting"] | None = None
    schedule_id: int | None = None


class JobRunner:
    """Job runner with bounded recording concurrency."""

    def __init__(self, *, worker=None, max_concurrent_recordings: int | None = None):
        self._worker = worker
        settings = get_settings()
        configured_limit = (
            max_concurrent_recordings if max_concurrent_recordings is not None else settings.max_concurrent_recordings
        )
        self._max_concurrent_recordings = max(1, int(configured_limit))
        self._recording_executor = RecordingExecutor(worker_provider=self._get_worker)
        self._upload_runner = YouTubeUploadRunner()
        self._schedule_queue = ScheduleRunQueue()
        self._direct_payloads: dict[str, DirectRecordingQueueItem] = {}
        self._retry_requests: dict[str, RecordingRetryRequest] = {}
        self._retry_tasks: dict[str, asyncio.Task] = {}
        self._retry_ready_at: dict[str, datetime] = {}
        self._active_tasks: set[asyncio.Task] = set()
        self._upload_tasks: dict[asyncio.Task, UploadRequest] = {}
        self._shutting_down = False

    @property
    def is_busy(self) -> bool:
        return self.active_count > 0

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)

    @property
    def max_concurrent_recordings(self) -> int:
        return self._max_concurrent_recordings

    @property
    def available_slots(self) -> int:
        return max(0, self._max_concurrent_recordings - self.active_count)

    @property
    def is_at_capacity(self) -> bool:
        return self.available_slots <= 0

    @property
    def current_schedule_id(self) -> int | None:
        return self._schedule_queue.current_schedule_id

    @property
    def queue_length(self) -> int:
        return self._schedule_queue.queue_length

    @property
    def queued_items(self) -> list[QueuedRunView]:
        return self._schedule_queue.queued_items()

    @property
    def retry_waiting_items(self) -> list[RetryWaitingView]:
        now = utc_now()
        items = []
        for job_id, request in self._retry_requests.items():
            ready_at = self._retry_ready_at.get(job_id, now + timedelta(seconds=request.delay_sec))
            retry_after_sec = max(0, math.ceil((ready_at - now).total_seconds()))
            items.append(
                RetryWaitingView(
                    job_id=job_id,
                    schedule_id=request.schedule_id,
                    status="retry_waiting",
                    retry_after_sec=retry_after_sec,
                    meeting_code=request.job.meeting_code,
                    display_name=request.job.display_name,
                )
            )
        return sorted(items, key=lambda item: item.retry_after_sec)

    @property
    def retry_waiting_count(self) -> int:
        return len(self._retry_requests)

    def is_retry_waiting_job(self, job_id: str) -> bool:
        return job_id in self._retry_requests

    def queue_schedule(self, schedule_id: int, manual_trigger: bool = False) -> QueueScheduleResult:
        """Queue a schedule to run."""
        result = self._schedule_queue.enqueue_schedule(
            schedule_id,
            manual_trigger=manual_trigger,
            can_start_now=self.available_slots > 0,
        )
        if not result.accepted:
            logger.warning(f"Schedule {schedule_id} is already running or queued")
            return result

        if result.status == "queued":
            logger.info(f"Schedule {schedule_id} waiting in queue (queue position: {result.queue_position})")

        self._drain_queues()
        return result

    def _ensure_queue_processor(self) -> None:
        """Start the schedule queue processor if no active processor exists."""
        self._drain_queues()

    async def _process_schedule_queue(self) -> None:
        """Compatibility wrapper: drain queues into available worker slots."""
        self._drain_queues()

    def _drain_queues(self) -> None:
        """Start queued work while capacity is available."""
        if self._shutting_down:
            return
        while self.available_slots > 0:
            queue_item = self._schedule_queue.pop_next()
            if queue_item:
                coro = self._run_queue_item(queue_item)
                try:
                    task = asyncio.create_task(coro)
                except Exception:
                    coro.close()
                    self._schedule_queue.restore_front(queue_item)
                    raise
                self._track_active_task(task)
                continue

            break

    def _track_active_task(self, task: asyncio.Task) -> None:
        self._active_tasks.add(task)
        try:
            task.add_done_callback(self._recording_task_done)
        except AttributeError:
            logger.debug("Recording task does not support done callbacks")

    def _recording_task_done(self, task: asyncio.Task) -> None:
        self._active_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Recording task canceled")
        except Exception as e:
            logger.error(f"Recording task failed: {e}")
        self._drain_queues()

    async def _run_queue_item(self, queue_item: QueuedRunItem) -> None:
        if queue_item.kind == "schedule":
            await self._run_schedule_queue_item(queue_item)
        elif queue_item.kind == "immediate":
            await self._run_direct_queue_item(queue_item)
        else:
            logger.error("Unknown queue item kind: %s", queue_item.kind)

    async def _run_schedule_queue_item(self, queue_item: QueuedRunItem) -> None:
        outcome: RecordingExecutionOutcome = None
        try:
            if queue_item.schedule_id is None:
                logger.error("Schedule queue item missing schedule_id")
                return
            outcome = await self._execute_schedule(
                queue_item.schedule_id,
                manual_trigger=queue_item.manual_trigger,
            )
        finally:
            self._schedule_queue.mark_done(queue_item)

        self._handle_recording_outcome(outcome)

    async def _run_direct_queue_item(self, queue_item: QueuedRunItem) -> None:
        if queue_item.job_id is None:
            logger.error("Immediate queue item missing job_id")
            return

        direct_payload = self._direct_payloads.pop(queue_item.job_id, None)
        if not direct_payload:
            logger.warning("Queued immediate job %s no longer has an execution payload", queue_item.job_id)
            self._mark_job_failed(
                queue_item.job_id,
                "Queued immediate job lost its execution payload",
                failure_stage="dispatch_recording",
            )
            return

        try:
            outcome = await self._run_recording_with_retry(
                job=direct_payload.job,
                schedule_id=direct_payload.schedule_id,
                meeting_end_time=direct_payload.meeting_end_time,
                youtube_enabled=direct_payload.youtube_enabled,
                youtube_privacy=direct_payload.youtube_privacy,
                meeting_name=direct_payload.meeting_name,
                retry_delay_sec=direct_payload.retry_delay_sec,
            )
        except Exception as e:
            logger.exception("Recording executor crashed for immediate job %s", queue_item.job_id)
            self._mark_job_failed(
                queue_item.job_id,
                f"Recording executor crashed: {e}",
                failure_stage="recording_executor",
            )
            return

        self._handle_recording_outcome(outcome)
        if direct_payload.schedule_id is not None and not isinstance(outcome, RecordingRetryRequest):
            self._mark_schedule_completed(direct_payload.schedule_id)

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
        return self._schedule_queue.is_schedule_active_or_queued(schedule_id) or any(
            request.schedule_id == schedule_id for request in self._retry_requests.values()
        )

    def cancel_queued_job_for_action(self, job_id: str) -> QueuedJobCancelResult:
        """Cancel queued work and report which queue source owned it."""
        retry_request = self._retry_requests.get(job_id)
        removed = self._schedule_queue.cancel_queued_immediate(job_id)
        retry_removed = self._cancel_delayed_retry_job(job_id)
        source: Literal["fifo", "retry_waiting"] | None = None
        schedule_id = None
        if removed:
            direct_payload = self._direct_payloads.pop(job_id, None)
            if direct_payload and direct_payload.schedule_id is not None:
                self._mark_schedule_completed(direct_payload.schedule_id)
                schedule_id = direct_payload.schedule_id
            source = "fifo"
        if retry_removed and retry_request and retry_request.schedule_id is not None:
            self._mark_schedule_completed(retry_request.schedule_id)
            schedule_id = retry_request.schedule_id
        if retry_removed:
            source = "retry_waiting"
        if removed or retry_removed:
            self._drain_queues()
        return QueuedJobCancelResult(removed=removed or retry_removed, source=source, schedule_id=schedule_id)

    def cancel_queued_job(self, job_id: str) -> bool:
        """Compatibility wrapper for canceling queued work by job id."""
        return self.cancel_queued_job_for_action(job_id).removed

    def cancel_queued_schedule(self, schedule_id: int) -> bool:
        """Cancel a queued schedule run without disabling the schedule."""
        removed = self._schedule_queue.cancel_queued_schedule(schedule_id)
        canceled_retry_job_ids = [
            job_id for job_id, payload in self._direct_payloads.items() if payload.schedule_id == schedule_id
        ]
        for job_id in canceled_retry_job_ids:
            self._direct_payloads.pop(job_id, None)
            self._mark_job_canceled(job_id, "Canceled queued schedule retry")
        if canceled_retry_job_ids:
            self._mark_schedule_completed(schedule_id)

        delayed_retry_job_ids = [
            job_id for job_id, request in self._retry_requests.items() if request.schedule_id == schedule_id
        ]
        for job_id in delayed_retry_job_ids:
            if self._cancel_delayed_retry_job(job_id):
                self._mark_job_canceled(job_id, "Canceled queued schedule retry")
        if delayed_retry_job_ids:
            self._mark_schedule_completed(schedule_id)

        if removed or canceled_retry_job_ids or delayed_retry_job_ids:
            self._drain_queues()
        return removed or bool(canceled_retry_job_ids) or bool(delayed_retry_job_ids)

    async def _execute_schedule(self, schedule_id: int, *, manual_trigger: bool = False) -> RecordingExecutionOutcome:
        """Execute a scheduled recording."""
        logger.info(f"Executing schedule {schedule_id}")
        SessionLocal = get_session_local()
        session = SessionLocal()
        schedule_started = False
        created_job_id = None
        outcome: RecordingExecutionOutcome = None

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
            created_job_id = job.job_id
            logger.info(f"Created job {job.job_id} for schedule {schedule_id}")

            outcome = await self._run_recording_with_retry(
                job=job,
                schedule_id=schedule_id,
                meeting_end_time=meeting_end_time,
                youtube_enabled=schedule.youtube_enabled,
                youtube_privacy=schedule.youtube_privacy,
                meeting_name=meeting.name,
            )
            return outcome
        except Exception as e:
            logger.error(f"Failed to execute schedule {schedule_id}: {e}")
            session.rollback()
            if created_job_id:
                self._mark_job_failed(
                    created_job_id,
                    f"Recording executor crashed: {e}",
                    failure_stage="recording_executor",
                )
            return None
        finally:
            session.close()
            if schedule_started and not isinstance(outcome, RecordingRetryRequest):
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

        self._direct_payloads[job.job_id] = DirectRecordingQueueItem(
            job=job,
            meeting_end_time=retry_window_end,
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            schedule_id=None,
        )
        self._schedule_queue.enqueue_immediate(
            job.job_id,
            can_start_now=self.available_slots > 0,
        )
        self._drain_queues()
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
        """Run an already-created job without queueing it."""
        outcome = await self._run_recording_with_retry(
            job=job,
            schedule_id=None,
            meeting_end_time=meeting_end_time,
            youtube_enabled=youtube_enabled,
            youtube_privacy=youtube_privacy,
            meeting_name=meeting_name,
        )

        self._handle_recording_outcome(outcome)

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

    def _mark_job_failed(
        self,
        job_id: str,
        error_message: str,
        *,
        failure_stage: str,
        error_code: str = ErrorCode.INTERNAL_ERROR.value,
    ) -> None:
        """Best-effort terminal update for jobs that fail outside worker result flow."""
        terminal_statuses = {
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELED.value,
        }
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            db_job = repo.get_by_job_id(job_id)
            if not db_job or db_job.status in terminal_statuses:
                return
            repo.update_status(
                job_id,
                JobStatus.FAILED.value,
                error_code=error_code,
                error_message=error_message,
                failure_stage=failure_stage,
                completed_at=utc_now(),
                end_reason="failed",
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to mark job %s as failed after runner error", job_id)
        finally:
            session.close()

    def _mark_job_canceled(self, job_id: str, error_message: str) -> None:
        """Best-effort terminal update for queued retry jobs canceled through schedule actions."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            db_job = repo.get_by_job_id(job_id)
            if not db_job or db_job.status in {
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELED.value,
            }:
                return
            repo.update_status(
                job_id,
                JobStatus.CANCELED.value,
                error_code=ErrorCode.CANCELED.value,
                error_message=error_message,
                completed_at=utc_now(),
                end_reason="canceled",
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to mark job %s as canceled", job_id)
        finally:
            session.close()

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
        retry_delay_sec: int | None = None,
    ) -> RecordingExecutionOutcome:
        """Run recording with retry using the recording executor."""
        kwargs = {
            "job": job,
            "schedule_id": schedule_id,
            "meeting_end_time": meeting_end_time,
            "youtube_enabled": youtube_enabled,
            "youtube_privacy": youtube_privacy,
            "meeting_name": meeting_name,
        }
        if retry_delay_sec is not None:
            kwargs["retry_delay_sec"] = retry_delay_sec
        return await self._recording_executor.run_with_retry(**kwargs)

    def _get_worker(self):
        return self._worker or get_worker()

    def _handle_recording_outcome(self, outcome: RecordingExecutionOutcome) -> None:
        if isinstance(outcome, RecordingRetryRequest):
            if self._shutting_down:
                self._mark_job_failed(
                    outcome.job.job_id,
                    "Recording retry skipped because server is shutting down",
                    failure_stage="recording_executor",
                )
                if outcome.schedule_id is not None:
                    self._mark_schedule_completed(outcome.schedule_id)
                return
            self._schedule_retry(outcome)
        elif isinstance(outcome, UploadRequest):
            self._start_upload_task(outcome)

    def _schedule_retry(self, retry_request: RecordingRetryRequest) -> None:
        job_id = retry_request.job.job_id
        old_task = self._retry_tasks.pop(job_id, None)
        if old_task:
            old_task.cancel()
        self._retry_requests[job_id] = retry_request
        self._retry_ready_at[job_id] = utc_now() + timedelta(seconds=retry_request.delay_sec)
        task = asyncio.create_task(self._delayed_requeue_retry(retry_request))
        self._retry_tasks[job_id] = task
        try:
            task.add_done_callback(
                lambda done_task, retry_job_id=job_id: self._retry_task_done(retry_job_id, done_task)
            )
        except AttributeError:
            logger.debug("Retry task does not support done callbacks")

    async def _delayed_requeue_retry(self, retry_request: RecordingRetryRequest) -> None:
        await asyncio.sleep(retry_request.delay_sec)
        job_id = retry_request.job.job_id
        if self._shutting_down:
            self._forget_retry_if_current(job_id, retry_request)
            return
        if self._retry_requests.get(job_id) is not retry_request:
            return

        self._retry_requests.pop(job_id, None)
        self._retry_tasks.pop(job_id, None)
        self._retry_ready_at.pop(job_id, None)
        self._direct_payloads[job_id] = DirectRecordingQueueItem(
            job=retry_request.job,
            meeting_end_time=retry_request.meeting_end_time,
            youtube_enabled=retry_request.youtube_enabled,
            youtube_privacy=retry_request.youtube_privacy,
            meeting_name=retry_request.meeting_name,
            schedule_id=retry_request.schedule_id,
            retry_delay_sec=retry_request.next_retry_delay_sec,
        )
        self._schedule_queue.enqueue_immediate(
            job_id,
            can_start_now=self.available_slots > 0,
            schedule_id=retry_request.schedule_id,
        )
        self._drain_queues()

    def _retry_task_done(self, job_id: str, task: asyncio.Task) -> None:
        if self._retry_tasks.get(job_id) is task:
            self._retry_tasks.pop(job_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Delayed retry canceled for job %s", job_id)
        except Exception as e:
            self._retry_requests.pop(job_id, None)
            self._retry_ready_at.pop(job_id, None)
            logger.error("Delayed retry failed for job %s: %s", job_id, e)

    def _cancel_delayed_retry_job(self, job_id: str) -> bool:
        removed = self._retry_requests.pop(job_id, None) is not None
        self._retry_ready_at.pop(job_id, None)
        task = self._retry_tasks.pop(job_id, None)
        if task:
            task.cancel()
            removed = True
        return removed

    def _forget_retry_if_current(self, job_id: str, retry_request: RecordingRetryRequest) -> None:
        if self._retry_requests.get(job_id) is retry_request:
            self._retry_requests.pop(job_id, None)
            self._retry_ready_at.pop(job_id, None)
        task = self._retry_tasks.get(job_id)
        if task and task.done():
            self._retry_tasks.pop(job_id, None)

    def _start_upload_task(self, upload_request: UploadRequest) -> None:
        if self._shutting_down:
            self._mark_upload_interrupted(upload_request.job_id)
            return
        task = asyncio.create_task(self._run_upload_task(upload_request))
        self._upload_tasks[task] = upload_request
        try:
            task.add_done_callback(self._upload_task_done)
        except AttributeError:
            logger.debug("Upload task does not support done callbacks")

    def _upload_task_done(self, task: asyncio.Task) -> None:
        upload_request = self._upload_tasks.pop(task, None)
        try:
            task.result()
        except asyncio.CancelledError:
            if upload_request:
                self._mark_upload_interrupted(upload_request.job_id)
        except Exception as e:
            logger.error("Upload task failed for job %s: %s", upload_request.job_id if upload_request else "?", e)

    async def shutdown(self, *, upload_timeout_sec: float = 15.0, recording_timeout_sec: float = 10.0) -> None:
        """Stop queue draining and settle tracked delayed retries, recordings, and uploads."""
        self._shutting_down = True

        retry_tasks = list(self._retry_tasks.values())
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
        self._retry_requests.clear()
        self._retry_ready_at.clear()

        active_tasks = list(self._active_tasks)
        if active_tasks:
            worker = self._get_worker()
            for job in getattr(worker, "active_jobs", []):
                try:
                    worker.request_cancel(job.job_id)
                except Exception:
                    logger.exception("Failed to request cancellation for active job %s during shutdown", job.job_id)

            done, pending = await asyncio.wait(active_tasks, timeout=recording_timeout_sec)
            for task in done:
                self._active_tasks.discard(task)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                for task in pending:
                    self._active_tasks.discard(task)

        upload_tasks = list(self._upload_tasks.keys())
        if not upload_tasks:
            return

        done, pending = await asyncio.wait(upload_tasks, timeout=upload_timeout_sec)
        for task in done:
            self._upload_task_done(task)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            for task in pending:
                upload_request = self._upload_tasks.pop(task, None)
                if upload_request:
                    self._mark_upload_interrupted(upload_request.job_id)

    def _mark_upload_interrupted(self, job_id: str) -> None:
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            db_job = repo.get_by_job_id(job_id)
            if not db_job or db_job.status != JobStatus.UPLOADING.value:
                return
            repo.update_status(
                job_id,
                JobStatus.SUCCEEDED.value,
                error_message="YouTube upload interrupted by server shutdown",
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to restore interrupted upload job %s", job_id)
        finally:
            session.close()

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
