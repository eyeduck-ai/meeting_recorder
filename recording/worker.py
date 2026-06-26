import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from database.models import ErrorCode, JobStatus
from recording.activity import LiveMediaActivityProbe
from recording.capacity_guard import RecordingCapacityError, RecordingCapacityGuard, RecordingCapacityReservation
from recording.job_types import RecordingJob as _RecordingJob
from recording.job_types import RecordingResult as _RecordingResult
from recording.monitor import RecordingMonitor
from recording.runtime_resources import RuntimeResourceAllocator, RuntimeResourceLease
from recording.session import RecordingSession
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)

_PROVIDER_JOIN_FAILURE_STAGES = {"join_meeting", "admit_or_fail"}


@dataclass
class ActiveRecordingState:
    """Mutable runtime state for one active recording job."""

    job: _RecordingJob
    status: JobStatus = JobStatus.QUEUED
    cancel_requested: bool = False
    finish_requested: bool = False
    started_at: datetime = field(default_factory=utc_now)


class RecordingWorker:
    """Recording worker that orchestrates the entire recording process."""

    def __init__(
        self,
        *,
        resource_allocator: RuntimeResourceAllocator | None = None,
        capacity_guard: RecordingCapacityGuard | None = None,
    ):
        self._active_jobs: dict[str, ActiveRecordingState] = {}
        self._current_job: _RecordingJob | None = None
        self._status_callback: Callable[[str, JobStatus], None] | None = None
        self._resource_allocator = resource_allocator or RuntimeResourceAllocator()
        self._capacity_guard = capacity_guard or RecordingCapacityGuard()

    @property
    def active_jobs(self) -> list[_RecordingJob]:
        return [state.job for state in self._active_jobs.values()]

    @property
    def active_count(self) -> int:
        return len(self._active_jobs)

    def set_status_callback(self, callback: Callable[[str, JobStatus], None]) -> None:
        self._status_callback = callback

    def _update_status(self, status: JobStatus, job_id: str) -> None:
        if job_id in self._active_jobs:
            self._active_jobs[job_id].status = status
        if self._status_callback:
            try:
                self._status_callback(job_id, status)
            except Exception as e:
                logger.warning(f"Status callback error: {e}")

    def request_cancel(self, job_id: str | None = None) -> bool:
        state = self._resolve_active_state(job_id)
        if state:
            state.cancel_requested = True
            return True
        return False

    def request_finish(self, job_id: str | None = None) -> bool:
        state = self._resolve_active_state(job_id)
        if state:
            state.finish_requested = True
            return True
        return False

    def is_job_active(self, job_id: str) -> bool:
        return job_id in self._active_jobs

    def _resolve_active_state(self, job_id: str | None = None) -> ActiveRecordingState | None:
        if job_id:
            return self._active_jobs.get(job_id)
        if self._current_job and self._current_job.job_id in self._active_jobs:
            return self._active_jobs[self._current_job.job_id]
        if self._active_jobs:
            return max(self._active_jobs.values(), key=lambda state: state.started_at)
        return None

    def _is_cancel_requested(self, job_id: str) -> bool:
        state = self._active_jobs.get(job_id)
        return bool(state.cancel_requested if state else False)

    def _is_finish_requested(self, job_id: str) -> bool:
        state = self._active_jobs.get(job_id)
        return bool(state.finish_requested if state else False)

    def _set_current_job(self, job: _RecordingJob | None) -> None:
        self._current_job = job

    def _is_provider_join_failure(self, result: _RecordingResult) -> bool:
        return bool(result.error_code and result.failure_stage in _PROVIDER_JOIN_FAILURE_STAGES)

    def _can_fallback_to_normal_browser(self, job: _RecordingJob, result: _RecordingResult) -> bool:
        return (
            job.recording_browser_mode == "app"
            and (job.resolved_browser_mode in (None, "app"))
            and not job.browser_fallback_used
            and result.recording_started_at is None
            and not self._is_provider_join_failure(result)
        )

    def _build_normal_browser_fallback_job(self, job: _RecordingJob, reason: str) -> _RecordingJob:
        fallback_crop_mode = job.recording_crop_mode if job.recording_crop_mode != "off" else "auto"
        return replace(
            job,
            resolved_browser_mode="normal",
            recording_crop_mode=fallback_crop_mode,
            browser_fallback_used=True,
            browser_fallback_reason=reason,
            browser_fallback_attempts=job.browser_fallback_attempts + 1,
        )

    async def record(self, job: _RecordingJob) -> _RecordingResult:
        """Execute a recording job."""
        result = _RecordingResult(
            job_id=job.job_id,
            status=JobStatus.STARTING,
            attempt_no=job.attempt_no,
            start_time=utc_now(),
        )
        current_job = job
        runtime_resources: RuntimeResourceLease | None = None
        capacity_reservation: RecordingCapacityReservation | None = None

        try:
            try:
                capacity_reservation = await self._capacity_guard.reserve(job)
            except RecordingCapacityError as e:
                result.status = JobStatus.FAILED
                result.error_code = ErrorCode.DISK_FULL.value
                result.error_message = str(e)
                result.failure_stage = "prepare_runtime"
                result.end_reason = "failed"
                result.end_time = utc_now()
                return result

            try:
                runtime_resources = await self._resource_allocator.acquire(job.job_id)
            except Exception as e:
                result.status = JobStatus.FAILED
                result.error_code = ErrorCode.VIRTUAL_ENV_ERROR.value
                result.error_message = str(e)
                result.failure_stage = "prepare_runtime"
                result.end_reason = "failed"
                result.end_time = utc_now()
                return result
            self._active_jobs[job.job_id] = ActiveRecordingState(job=current_job, status=JobStatus.STARTING)
            while True:
                self._active_jobs[job.job_id].job = current_job
                self._set_current_job(current_job)
                self._reset_result_for_attempt(result, current_job)
                current_job.output_dir.mkdir(parents=True, exist_ok=True)
                self._update_status(JobStatus.STARTING, current_job.job_id)

                fallback_job = await self._record_attempt(current_job, result, runtime_resources)
                if fallback_job is None:
                    return result
                current_job = fallback_job
        finally:
            self._active_jobs.pop(job.job_id, None)
            await self._resource_allocator.release(job.job_id)
            if capacity_reservation is not None:
                await self._capacity_guard.release(job.job_id)
            latest = self._resolve_active_state()
            self._set_current_job(latest.job if latest else None)

    def _reset_result_for_attempt(self, result: _RecordingResult, job: _RecordingJob) -> None:
        result.status = JobStatus.STARTING
        result.attempt_no = job.attempt_no
        result.recording_info = None
        result.output_path = None
        result.raw_output_path = None
        result.trimmed_output_path = None
        result.trim_start_sec = None
        result.trim_end_sec = None
        result.trim_status = None
        result.trim_reason = None
        result.trim_diagnostics = None
        result.dynamic_extension_stop_reason = None
        result.diagnostic_data = None
        result.error_code = None
        result.error_message = None
        result.joined_at = None
        result.recording_started_at = None
        result.recording_stopped_at = None
        result.end_time = None
        result.end_reason = None
        result.failure_stage = None
        result.ffmpeg_exit_code = None
        result.runtime_summary = None

    async def _record_attempt(
        self,
        job: _RecordingJob,
        result: _RecordingResult,
        runtime_resources: RuntimeResourceLease | None = None,
    ) -> _RecordingJob | None:
        """Execute one browser attempt for a recording job."""

        session_cleaned = False
        try:
            session = RecordingSession(job, runtime_resources=runtime_resources)
        except Exception as e:
            result.status = JobStatus.FAILED
            result.error_code = ErrorCode.INTERNAL_ERROR.value
            result.error_message = str(e)
            result.failure_stage = "prepare_runtime"
            result.end_reason = "failed"
            result.end_time = utc_now()
            self._update_status(JobStatus.FAILED, job.job_id)
            return None

        try:
            session.begin_stage("prepare_runtime")
            await session.prepare_runtime()
            session.end_stage("prepare_runtime")

            if self._is_cancel_requested(job.job_id):
                raise asyncio.CancelledError("Job cancelled")

            self._update_status(JobStatus.JOINING, job.job_id)
            session.begin_stage("join_meeting")
            join_result = await session.join_meeting()
            if join_result.in_lobby:
                session.end_stage("join_meeting", status="lobby")
            elif not join_result.success:
                session.end_stage("join_meeting", status="error")
                result.failure_stage = "join_meeting"
                result.error_code = join_result.error_code or ErrorCode.JOIN_FAILED.value
                raise RuntimeError(f"Failed to join meeting: {join_result.error_code} - {join_result.error_message}")
            else:
                session.end_stage("join_meeting")

            if join_result.in_lobby:
                self._update_status(JobStatus.WAITING_LOBBY, job.job_id)
                session.begin_stage("admit_or_fail")
                admitted = await session.wait_for_lobby_admission()
                if not admitted:
                    session.end_stage("admit_or_fail", status="error")
                    result.failure_stage = "admit_or_fail"
                    result.error_code = ErrorCode.LOBBY_TIMEOUT.value
                    raise RuntimeError("Lobby timeout - not admitted to meeting")

                final_check = await session.ensure_joined()
                if not final_check.success:
                    session.end_stage("admit_or_fail", status="error")
                    result.failure_stage = "admit_or_fail"
                    result.error_code = ErrorCode.NEVER_JOINED.value
                    raise RuntimeError("Never joined meeting - verification failed after lobby wait")
                session.end_stage("admit_or_fail")

            result.joined_at = utc_now()
            session.begin_stage("dismiss_overlays_joined")
            await session.dismiss_provider_overlays("dismiss_overlays_joined")
            session.end_stage("dismiss_overlays_joined")

            if self._is_cancel_requested(job.job_id):
                raise asyncio.CancelledError("Job cancelled")

            session.begin_stage("set_layout")
            await session.set_layout("speaker")
            session.end_stage("set_layout")

            session.begin_stage("dismiss_overlays_pre_capture")
            await session.dismiss_provider_overlays("dismiss_overlays_pre_capture")
            session.end_stage("dismiss_overlays_pre_capture")

            session.begin_stage("prepare_capture_surface")
            await session.prepare_capture_surface()
            session.end_stage("prepare_capture_surface")

            if job.deadline_at:
                deadline_at = ensure_utc(job.deadline_at)
                if deadline_at:
                    remaining = int((deadline_at - utc_now()).total_seconds())
                    if remaining <= 0:
                        hard_deadline_at = ensure_utc(job.hard_deadline_at)
                        if hard_deadline_at and hard_deadline_at > utc_now():
                            job.deadline_at = None
                            job.duration_sec = 1
                            logger.info(
                                "Fixed duration deadline already passed; using hard deadline %s for retry window",
                                hard_deadline_at.isoformat(),
                            )
                        else:
                            raise RuntimeError("Recording deadline already passed")
                    else:
                        job.duration_sec = remaining
                        logger.info(f"Fixed duration deadline: {deadline_at.isoformat()} (remaining {remaining}s)")

            if job.hard_deadline_at:
                hard_deadline_at = ensure_utc(job.hard_deadline_at)
                if hard_deadline_at:
                    hard_remaining = int((hard_deadline_at - utc_now()).total_seconds())
                    if hard_remaining <= 0:
                        raise RuntimeError("Recording hard deadline already passed")
                    if job.duration_sec > hard_remaining:
                        job.duration_sec = hard_remaining
                        logger.info(
                            "Hard recording deadline: %s (remaining %ss)",
                            hard_deadline_at.isoformat(),
                            hard_remaining,
                        )

            if self._is_finish_requested(job.job_id):
                raise asyncio.CancelledError("Finish requested before recording started")

            self._update_status(JobStatus.RECORDING, job.job_id)
            result.recording_started_at = utc_now()

            session.begin_stage("start_capture")
            await session.start_capture()
            session.end_stage("start_capture")

            session.begin_stage("dismiss_overlays_monitor")
            await session.dismiss_provider_overlays("dismiss_overlays_monitor")
            session.end_stage("dismiss_overlays_monitor")

            session.begin_stage("monitor_recording")
            try:
                await session.probe_provider_state("monitor_recording")
            except Exception as e:
                logger.warning(f"Failed to record provider state at recording start: {e}")
            monitor_result = await self._monitor_recording(
                session=session,
                job=job,
                ffmpeg_stall_timeout_sec=job.ffmpeg_stall_timeout_sec,
                ffmpeg_stall_grace_sec=job.ffmpeg_stall_grace_sec,
            )
            result.end_reason = monitor_result[0]
            result.ffmpeg_exit_code = monitor_result[1]
            result.dynamic_extension_stop_reason = monitor_result[2] if len(monitor_result) > 2 else None
            session.end_stage("monitor_recording")

            self._update_status(JobStatus.FINALIZING, job.job_id)
            session.begin_stage("finalize_capture")
            result.recording_info = await session.finalize_capture()
            session.end_stage("finalize_capture")
            result.recording_stopped_at = utc_now()
            session_cleaned = await self._cleanup_session(session, job.job_id, context="post_capture")
            if result.recording_info:
                result.raw_output_path = result.recording_info.output_path
                result.output_path = result.recording_info.output_path

            result.status = JobStatus.SUCCEEDED
            result.end_time = utc_now()
            result.runtime_summary = session.build_runtime_summary(
                end_reason=result.end_reason,
                recording_info=result.recording_info,
                dynamic_extension_stop_reason=result.dynamic_extension_stop_reason,
            )
            if job.job_id in self._active_jobs:
                self._active_jobs[job.job_id].status = JobStatus.SUCCEEDED

            logger.info(f"Recording completed successfully: {result.recording_info.output_path}")

        except asyncio.CancelledError:
            active_stage = session.current_stage()
            if active_stage:
                session.end_stage(active_stage, status="canceled")
            result.status = JobStatus.CANCELED
            result.error_code = ErrorCode.CANCELED.value
            result.error_message = "Job was cancelled"
            result.end_time = utc_now()
            result.end_reason = "canceled"
            result.failure_stage = result.failure_stage or active_stage
            result.ffmpeg_exit_code = session.process_returncode()
            result.runtime_summary = session.build_runtime_summary(
                failure_stage=result.failure_stage,
                ffmpeg_exit_code=result.ffmpeg_exit_code,
                end_reason=result.end_reason,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            self._update_status(JobStatus.CANCELED, job.job_id)
            logger.info("Recording cancelled")

        except Exception as e:
            active_stage = session.current_stage()
            if active_stage:
                session.end_stage(active_stage, status="error")
            if self._can_fallback_to_normal_browser(job, result):
                fallback_reason = f"{active_stage or 'unknown'}: {e}"
                logger.warning(
                    "App browser mode failed before capture; retrying job %s in normal browser mode: %s",
                    job.job_id,
                    fallback_reason,
                )
                result.runtime_summary = session.build_runtime_summary(
                    failure_stage=active_stage,
                    ffmpeg_exit_code=session.process_returncode(),
                    end_reason="browser_fallback",
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    error_message=fallback_reason,
                )
                fallback_job = self._build_normal_browser_fallback_job(job, fallback_reason)
                return fallback_job

            result.status = JobStatus.FAILED
            if not result.error_code:
                if "ffmpeg" in str(e).lower():
                    result.error_code = ErrorCode.FFMPEG_ERROR.value
                else:
                    result.error_code = ErrorCode.INTERNAL_ERROR.value
            result.error_message = str(e)
            result.end_time = utc_now()
            result.end_reason = "failed"
            result.failure_stage = result.failure_stage or active_stage
            result.ffmpeg_exit_code = session.process_returncode()
            result.runtime_summary = session.build_runtime_summary(
                failure_stage=result.failure_stage,
                ffmpeg_exit_code=result.ffmpeg_exit_code,
                end_reason=result.end_reason,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            diagnostics_dir = getattr(session, "diagnostics_dir", None)
            self._update_status(JobStatus.FAILED, job.job_id)
            logger.error(f"Recording failed: {e} (stage={result.failure_stage}, diagnostics={diagnostics_dir})")
            result.diagnostic_data = await session.collect_diagnostics(
                error_code=result.error_code,
                error_message=result.error_message,
                runtime_summary=result.runtime_summary,
            )

        finally:
            try:
                await self._save_detection_logs(job, result)
            except Exception as e:
                logger.warning(f"Failed to save detection logs: {e}")

            if not session_cleaned:
                session_cleaned = await self._cleanup_session(session, job.job_id, context="finalizer")

        return None

    async def _cleanup_session(self, session: RecordingSession, job_id: str, *, context: str) -> bool:
        """Best-effort cleanup that never rewrites the recording result."""
        try:
            await session.cleanup()
            return True
        except Exception as e:
            logger.warning("Recording session cleanup failed for job %s during %s: %s", job_id, context, e)
            return False

    async def _save_detection_logs(
        self,
        job: _RecordingJob,
        result: _RecordingResult,
    ) -> None:
        """Persist media activity decisions."""
        has_activity_log = result.trim_status is not None
        has_extension_log = result.dynamic_extension_stop_reason is not None
        if not has_activity_log and not has_extension_log:
            return

        from database.models import DetectionLog
        from database.models import RecordingJob as DBJob
        from database.session import get_session_local

        SessionLocal = get_session_local()
        db_session = SessionLocal()
        try:
            db_job = db_session.query(DBJob).filter(DBJob.job_id == job.job_id).first()
            if not db_job:
                return
            if has_extension_log:
                db_session.add(
                    DetectionLog(
                        job_id=db_job.id,
                        detector_type="dynamic_extension",
                        detected=result.dynamic_extension_stop_reason != "fixed_duration_reached",
                        confidence=1.0,
                        reason=result.dynamic_extension_stop_reason,
                        attempt_no=job.attempt_no,
                        triggered_at=utc_now(),
                    )
                )
            if has_activity_log:
                db_session.add(
                    DetectionLog(
                        job_id=db_job.id,
                        detector_type="media_activity",
                        detected=result.trim_status == "trimmed",
                        confidence=1.0,
                        reason=result.trim_reason,
                        attempt_no=job.attempt_no,
                        triggered_at=utc_now(),
                    )
                )
            db_session.commit()
            logger.info("Saved detection/activity logs for job %s", job.job_id)
        finally:
            db_session.close()

    async def _monitor_recording(
        self,
        *,
        session: RecordingSession,
        job: _RecordingJob,
        ffmpeg_stall_timeout_sec: int,
        ffmpeg_stall_grace_sec: int,
    ) -> tuple[str, int | None, str | None]:
        """Monitor the recording loop until it should stop."""
        media_activity_probe = LiveMediaActivityProbe(job.activity_config) if job.dynamic_extension_enabled else None
        monitor = RecordingMonitor(
            session=session,
            job=job,
            media_activity_probe=media_activity_probe,
            is_cancel_requested=lambda: self._is_cancel_requested(job.job_id),
            is_finish_requested=lambda: self._is_finish_requested(job.job_id),
            ffmpeg_stall_timeout_sec=ffmpeg_stall_timeout_sec,
            ffmpeg_stall_grace_sec=ffmpeg_stall_grace_sec,
        )
        end_reason, ffmpeg_exit_code = await monitor.run()
        return end_reason, ffmpeg_exit_code, monitor.dynamic_extension_stop_reason


_worker_instance: RecordingWorker | None = None


def get_worker() -> RecordingWorker:
    """Get the global worker instance."""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = RecordingWorker()
    return _worker_instance


def set_worker_instance(worker: RecordingWorker) -> None:
    """Set the compatibility worker singleton."""
    global _worker_instance
    _worker_instance = worker


def reset_worker_instance() -> None:
    """Clear the compatibility worker singleton."""
    global _worker_instance
    _worker_instance = None
