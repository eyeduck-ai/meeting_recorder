import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import get_settings
from database.models import ErrorCode, JobStatus
from recording.activity import ActivityConfig, LiveMediaActivityProbe, RecordingActivityAnalyzer, trim_recording
from recording.detection import DetectionConfig, DetectionOrchestrator
from recording.detectors import AudioSilenceDetector, create_default_detectors
from recording.ffmpeg_pipeline import RecordingInfo
from recording.monitor import RecordingMonitor
from recording.session import RecordingSession
from services.runtime_config import RuntimeRecordingConfig, get_runtime_config_service
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)


@dataclass
class RecordingResult:
    """Result of a recording job."""

    job_id: str
    status: JobStatus
    attempt_no: int = 1
    recording_info: RecordingInfo | None = None
    output_path: Path | None = None
    raw_output_path: Path | None = None
    trimmed_output_path: Path | None = None
    trim_start_sec: float | None = None
    trim_end_sec: float | None = None
    trim_status: str | None = None
    trim_reason: str | None = None
    trim_diagnostics: dict | None = None
    dynamic_extension_stop_reason: str | None = None
    diagnostic_data: object | None = None
    error_code: str | None = None
    error_message: str | None = None
    start_time: datetime | None = None
    joined_at: datetime | None = None
    recording_started_at: datetime | None = None
    recording_stopped_at: datetime | None = None
    end_time: datetime | None = None
    end_reason: str | None = None
    failure_stage: str | None = None
    ffmpeg_exit_code: int | None = None
    runtime_summary: dict | None = None


@dataclass
class RecordingJob:
    """A recording job configuration."""

    job_id: str
    provider: str
    meeting_code: str
    display_name: str
    duration_sec: int
    output_dir: Path
    attempt_no: int = 1
    deadline_at: datetime | None = None
    base_url: str | None = None
    password: str | None = None
    lobby_wait_sec: int = 900
    resolution_w: int = 1920
    resolution_h: int = 1080
    recording_browser_mode: str = "app"
    resolved_browser_mode: str | None = None
    recording_crop_mode: str = "off"
    recording_crop_top_px: int = 0
    browser_fallback_used: bool = False
    browser_fallback_reason: str | None = None
    browser_fallback_attempts: int = 0
    diagnostics_dir: Path | None = None
    ffmpeg_stall_timeout_sec: int = 120
    ffmpeg_stall_grace_sec: int = 30
    duration_mode: str = "fixed"
    dry_run: bool = False
    min_duration_sec: int | None = None
    stillness_timeout_sec: int = 180
    smart_trim_enabled: bool = True
    dynamic_extension_enabled: bool = True
    dynamic_extension_idle_sec: int = 300
    dynamic_extension_max_sec: int = 3600
    activity_config: ActivityConfig = field(default_factory=ActivityConfig)

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.resolution_w, self.resolution_h)

    @classmethod
    def create(
        cls,
        provider: str,
        meeting_code: str,
        display_name: str,
        duration_sec: int,
        output_dir: Path | None = None,
        job_id: str | None = None,
        attempt_no: int = 1,
        **kwargs,
    ) -> "RecordingJob":
        """Create a new recording job with generated ID."""
        runtime_config: RuntimeRecordingConfig | None = kwargs.get("runtime_config")
        if runtime_config is None:
            runtime_config = get_runtime_config_service(settings=get_settings()).get_recording_config(
                lobby_wait_sec=kwargs.get("lobby_wait_sec"),
                resolution_w=kwargs.get("resolution_w"),
                resolution_h=kwargs.get("resolution_h"),
            )
        resolved_job_id = job_id or str(uuid.uuid4())[:8]
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

        if output_dir is None:
            output_dir = runtime_config.recordings_dir / f"{timestamp}_{resolved_job_id}"

        return cls(
            job_id=resolved_job_id,
            provider=provider,
            meeting_code=meeting_code,
            display_name=display_name,
            duration_sec=duration_sec,
            output_dir=output_dir,
            attempt_no=attempt_no,
            deadline_at=kwargs.get("deadline_at"),
            lobby_wait_sec=runtime_config.lobby_wait_sec,
            resolution_w=runtime_config.resolution_w,
            resolution_h=runtime_config.resolution_h,
            recording_browser_mode=runtime_config.recording_browser_mode,
            recording_crop_mode=runtime_config.recording_crop_mode,
            recording_crop_top_px=runtime_config.recording_crop_top_px,
            diagnostics_dir=runtime_config.diagnostics_dir / resolved_job_id,
            ffmpeg_stall_timeout_sec=runtime_config.ffmpeg_stall_timeout_sec,
            ffmpeg_stall_grace_sec=runtime_config.ffmpeg_stall_grace_sec,
            base_url=kwargs.get("base_url"),
            password=kwargs.get("password"),
            duration_mode=kwargs.get("duration_mode", "fixed"),
            dry_run=kwargs.get("dry_run", False),
            min_duration_sec=kwargs.get("min_duration_sec"),
            stillness_timeout_sec=kwargs.get("stillness_timeout_sec", 180),
            smart_trim_enabled=runtime_config.smart_trim_enabled,
            dynamic_extension_enabled=runtime_config.dynamic_extension_enabled,
            dynamic_extension_idle_sec=runtime_config.dynamic_extension_idle_sec,
            dynamic_extension_max_sec=runtime_config.dynamic_extension_max_sec,
            activity_config=runtime_config.activity_config,
        )


class RecordingWorker:
    """Recording worker that orchestrates the entire recording process."""

    def __init__(self):
        self._current_job: RecordingJob | None = None
        self._status: JobStatus = JobStatus.QUEUED
        self._cancel_requested: bool = False
        self._finish_requested: bool = False
        self._status_callback: Callable[[str, JobStatus], None] | None = None

    @property
    def is_busy(self) -> bool:
        return self._current_job is not None

    @property
    def current_status(self) -> JobStatus:
        return self._status

    def set_status_callback(self, callback: Callable[[str, JobStatus], None]) -> None:
        self._status_callback = callback

    def _update_status(self, status: JobStatus) -> None:
        self._status = status
        if self._status_callback and self._current_job:
            try:
                self._status_callback(self._current_job.job_id, status)
            except Exception as e:
                logger.warning(f"Status callback error: {e}")

    def _load_detection_config(self) -> DetectionConfig:
        """Load detection configuration from database."""
        from database.models import AppSettings
        from database.session import get_session_local

        try:
            SessionLocal = get_session_local()
            session = SessionLocal()
            try:
                record = session.query(AppSettings).filter(AppSettings.key == "detection_config").first()
                if record:
                    data = json.loads(record.value)
                    return DetectionConfig(
                        text_indicator_enabled=data.get("text_indicator_enabled", True),
                        video_element_enabled=data.get("video_element_enabled", True),
                        webrtc_connection_enabled=data.get("webrtc_connection_enabled", True),
                        screen_freeze_enabled=data.get("screen_freeze_enabled", False),
                        audio_silence_enabled=data.get("audio_silence_enabled", False),
                        url_change_enabled=data.get("url_change_enabled", True),
                        screen_freeze_threshold=data.get("screen_freeze_threshold", 0.98),
                        screen_freeze_timeout_sec=data.get("screen_freeze_timeout_sec", 60),
                        audio_silence_timeout_sec=data.get("audio_silence_timeout_sec", 120),
                        audio_silence_threshold=data.get("audio_silence_threshold", 0.05),
                        min_detectors_agree=data.get("min_detectors_agree", 1),
                    )
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"Failed to load detection config from database: {e}")

        return DetectionConfig()

    def request_cancel(self) -> bool:
        if self._current_job:
            self._cancel_requested = True
            return True
        return False

    def request_finish(self) -> bool:
        if self._current_job:
            self._finish_requested = True
            return True
        return False

    def _can_fallback_to_normal_browser(self, job: RecordingJob, result: RecordingResult) -> bool:
        return (
            job.recording_browser_mode == "app"
            and (job.resolved_browser_mode in (None, "app"))
            and not job.browser_fallback_used
            and result.recording_started_at is None
        )

    def _build_normal_browser_fallback_job(self, job: RecordingJob, reason: str) -> RecordingJob:
        fallback_crop_mode = job.recording_crop_mode if job.recording_crop_mode != "off" else "auto"
        return replace(
            job,
            resolved_browser_mode="normal",
            recording_crop_mode=fallback_crop_mode,
            browser_fallback_used=True,
            browser_fallback_reason=reason,
            browser_fallback_attempts=job.browser_fallback_attempts + 1,
        )

    async def record(self, job: RecordingJob) -> RecordingResult:
        """Execute a recording job."""
        self._cancel_requested = False
        self._finish_requested = False
        result = RecordingResult(
            job_id=job.job_id,
            status=JobStatus.STARTING,
            attempt_no=job.attempt_no,
            start_time=utc_now(),
        )
        current_job = job

        try:
            while True:
                self._current_job = current_job
                self._reset_result_for_attempt(result, current_job)
                current_job.output_dir.mkdir(parents=True, exist_ok=True)
                self._update_status(JobStatus.STARTING)

                fallback_job = await self._record_attempt(current_job, result)
                if fallback_job is None:
                    return result
                current_job = fallback_job
        finally:
            self._current_job = None

    def _reset_result_for_attempt(self, result: RecordingResult, job: RecordingJob) -> None:
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

    async def _record_attempt(self, job: RecordingJob, result: RecordingResult) -> RecordingJob | None:
        """Execute one browser attempt for a recording job."""

        session = RecordingSession(job)
        detection_orchestrator: DetectionOrchestrator | None = None

        try:
            session.begin_stage("prepare_runtime")
            await session.prepare_runtime()
            session.end_stage("prepare_runtime")

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            self._update_status(JobStatus.JOINING)
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
                self._update_status(JobStatus.WAITING_LOBBY)
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

            if self._cancel_requested:
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

            if job.duration_mode == "fixed" and job.deadline_at:
                deadline_at = ensure_utc(job.deadline_at)
                if deadline_at:
                    remaining = int((deadline_at - utc_now()).total_seconds())
                    if remaining <= 0:
                        raise RuntimeError("Recording deadline already passed")
                    job.duration_sec = remaining
                    logger.info(f"Fixed duration deadline: {deadline_at.isoformat()} (remaining {remaining}s)")

            if self._finish_requested:
                raise asyncio.CancelledError("Finish requested before recording started")

            self._update_status(JobStatus.RECORDING)
            result.recording_started_at = utc_now()

            session.begin_stage("start_capture")
            await session.start_capture()
            session.end_stage("start_capture")

            session.begin_stage("dismiss_overlays_monitor")
            await session.dismiss_provider_overlays("dismiss_overlays_monitor")
            session.end_stage("dismiss_overlays_monitor")

            if job.duration_mode == "auto":
                detection_config = self._load_detection_config()
                detection_config.screen_freeze_timeout_sec = job.stillness_timeout_sec
                detection_orchestrator = DetectionOrchestrator(detection_config)
                detection_orchestrator.set_dry_run(job.dry_run)
                for detector in create_default_detectors(detection_config):
                    if isinstance(detector, AudioSilenceDetector) and session.virtual_env:
                        detector.set_audio_source(session.virtual_env.pulse_monitor)
                    detection_orchestrator.register_detector(detector)
                await detection_orchestrator.setup_all(session.page)
                logger.info(
                    "Auto-detection mode enabled "
                    f"(dry_run={job.dry_run}, stillness_timeout={job.stillness_timeout_sec}s)"
                )

            session.begin_stage("monitor_recording")
            try:
                await session.probe_provider_state("monitor_recording")
            except Exception as e:
                logger.warning(f"Failed to record provider state at recording start: {e}")
            monitor_result = await self._monitor_recording(
                session=session,
                job=job,
                detection_orchestrator=detection_orchestrator,
                ffmpeg_stall_timeout_sec=job.ffmpeg_stall_timeout_sec,
                ffmpeg_stall_grace_sec=job.ffmpeg_stall_grace_sec,
            )
            result.end_reason = monitor_result[0]
            result.ffmpeg_exit_code = monitor_result[1]
            result.dynamic_extension_stop_reason = monitor_result[2] if len(monitor_result) > 2 else None
            session.end_stage("monitor_recording")

            self._update_status(JobStatus.FINALIZING)
            session.begin_stage("finalize_capture")
            result.recording_info = await session.finalize_capture()
            session.end_stage("finalize_capture")
            result.recording_stopped_at = utc_now()
            await self._apply_smart_trim(session, job, result)

            result.status = JobStatus.SUCCEEDED
            result.end_time = utc_now()
            result.runtime_summary = session.build_runtime_summary(
                end_reason=result.end_reason,
                recording_info=result.recording_info,
                trim_summary=self._build_trim_summary(result),
                dynamic_extension_stop_reason=result.dynamic_extension_stop_reason,
            )
            self._update_status(JobStatus.SUCCEEDED)

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
            self._update_status(JobStatus.CANCELED)
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
            self._update_status(JobStatus.FAILED)
            diagnostics_dir = getattr(session, "diagnostics_dir", None)
            logger.error(f"Recording failed: {e} (stage={result.failure_stage}, diagnostics={diagnostics_dir})")
            result.diagnostic_data = await session.collect_diagnostics(
                error_code=result.error_code,
                error_message=result.error_message,
                runtime_summary=result.runtime_summary,
            )

        finally:
            try:
                await self._save_detection_logs(job, result, detection_orchestrator)
            except Exception as e:
                logger.warning(f"Failed to save detection logs: {e}")

            await session.cleanup()

        return None

    async def _save_detection_logs(
        self,
        job: RecordingJob,
        result: RecordingResult,
        detection_orchestrator: DetectionOrchestrator | None,
    ) -> None:
        """Persist provider detection and media activity decisions."""
        provider_logs = detection_orchestrator.detection_log if detection_orchestrator else []
        has_activity_log = result.trim_status is not None
        has_extension_log = result.dynamic_extension_stop_reason is not None
        if not provider_logs and not has_activity_log and not has_extension_log:
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
            for log_entry in provider_logs:
                db_session.add(
                    DetectionLog(
                        job_id=db_job.id,
                        detector_type=log_entry.detector_type.value,
                        detected=log_entry.detected,
                        confidence=log_entry.confidence,
                        reason=log_entry.reason,
                        attempt_no=job.attempt_no,
                        triggered_at=log_entry.timestamp,
                    )
                )
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
        job: RecordingJob,
        detection_orchestrator: DetectionOrchestrator | None,
        ffmpeg_stall_timeout_sec: int,
        ffmpeg_stall_grace_sec: int,
    ) -> tuple[str, int | None, str | None]:
        """Monitor the recording loop until it should stop."""
        media_activity_probe = LiveMediaActivityProbe(job.activity_config) if job.dynamic_extension_enabled else None
        monitor = RecordingMonitor(
            session=session,
            job=job,
            detection_orchestrator=detection_orchestrator,
            media_activity_probe=media_activity_probe,
            is_cancel_requested=lambda: self._cancel_requested,
            is_finish_requested=lambda: self._finish_requested,
            ffmpeg_stall_timeout_sec=ffmpeg_stall_timeout_sec,
            ffmpeg_stall_grace_sec=ffmpeg_stall_grace_sec,
        )
        end_reason, ffmpeg_exit_code = await monitor.run()
        return end_reason, ffmpeg_exit_code, monitor.dynamic_extension_stop_reason

    async def _apply_smart_trim(
        self,
        session: RecordingSession,
        job: RecordingJob,
        result: RecordingResult,
    ) -> None:
        """Analyze and optionally create a trimmed preferred output."""
        if not result.recording_info:
            return

        raw_info = result.recording_info
        raw_path = raw_info.output_path
        result.raw_output_path = raw_path
        result.output_path = raw_path
        result.trim_start_sec = 0.0
        result.trim_end_sec = raw_info.duration_sec

        if not raw_path.exists():
            result.trim_status = "skipped"
            result.trim_reason = "raw recording file not available"
            return

        if not job.smart_trim_enabled:
            result.trim_status = "disabled"
            result.trim_reason = "smart trim disabled"
            return

        analyzer = RecordingActivityAnalyzer(job.activity_config)
        decision = await analyzer.analyze(raw_path)
        result.trim_start_sec = decision.trim_start_sec
        result.trim_end_sec = decision.trim_end_sec
        result.trim_status = decision.status
        result.trim_reason = decision.reason
        result.trim_diagnostics = decision.diagnostics

        if not decision.should_trim or decision.trim_end_sec is None:
            return

        trimmed_path = raw_path.with_name(f"{raw_path.stem}.trimmed{raw_path.suffix}")
        trimmed_info = await trim_recording(
            input_path=raw_path,
            output_path=trimmed_path,
            trim_start_sec=decision.trim_start_sec,
            trim_end_sec=decision.trim_end_sec,
            log_path=session.diagnostics_dir / "trim.log",
        )
        if not trimmed_info:
            result.trim_status = "failed"
            result.trim_reason = "trim command failed; raw recording retained"
            result.trimmed_output_path = None
            result.output_path = raw_path
            result.recording_info = raw_info
            return

        trimmed_info.start_time = raw_info.start_time + timedelta(seconds=decision.trim_start_sec)
        trimmed_info.end_time = raw_info.start_time + timedelta(seconds=decision.trim_end_sec)
        result.trimmed_output_path = trimmed_path
        result.output_path = trimmed_path
        result.recording_info = trimmed_info

    def _build_trim_summary(self, result: RecordingResult) -> dict:
        return {
            "raw_output_path": str(result.raw_output_path) if result.raw_output_path else None,
            "trimmed_output_path": str(result.trimmed_output_path) if result.trimmed_output_path else None,
            "trim_start_sec": result.trim_start_sec,
            "trim_end_sec": result.trim_end_sec,
            "trim_status": result.trim_status,
            "trim_reason": result.trim_reason,
            "diagnostics": result.trim_diagnostics,
        }


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
