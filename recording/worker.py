import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config.settings import get_settings
from database.models import ErrorCode, JobStatus
from recording.detection import DetectionConfig, DetectionOrchestrator
from recording.detectors import AudioSilenceDetector, create_default_detectors
from recording.ffmpeg_pipeline import RecordingInfo
from recording.session import RecordingSession
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)


@dataclass
class RecordingResult:
    """Result of a recording job."""

    job_id: str
    status: JobStatus
    attempt_no: int = 1
    recording_info: RecordingInfo | None = None
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
    duration_mode: str = "fixed"
    dry_run: bool = False
    min_duration_sec: int | None = None
    stillness_timeout_sec: int = 180

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
        settings = get_settings()
        resolved_job_id = job_id or str(uuid.uuid4())[:8]
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

        if output_dir is None:
            output_dir = settings.recordings_dir / f"{timestamp}_{resolved_job_id}"

        return cls(
            job_id=resolved_job_id,
            provider=provider,
            meeting_code=meeting_code,
            display_name=display_name,
            duration_sec=duration_sec,
            output_dir=output_dir,
            attempt_no=attempt_no,
            deadline_at=kwargs.get("deadline_at"),
            lobby_wait_sec=kwargs.get("lobby_wait_sec", settings.lobby_wait_sec),
            base_url=kwargs.get("base_url"),
            password=kwargs.get("password"),
            duration_mode=kwargs.get("duration_mode", "fixed"),
            dry_run=kwargs.get("dry_run", False),
            min_duration_sec=kwargs.get("min_duration_sec"),
            stillness_timeout_sec=kwargs.get("stillness_timeout_sec", 180),
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
        from database.models import AppSettings, get_session_local

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

    async def record(self, job: RecordingJob) -> RecordingResult:
        """Execute a recording job."""
        self._current_job = job
        self._cancel_requested = False
        self._finish_requested = False
        self._update_status(JobStatus.STARTING)

        result = RecordingResult(
            job_id=job.job_id,
            status=JobStatus.STARTING,
            attempt_no=job.attempt_no,
            start_time=utc_now(),
        )

        settings = get_settings()
        job.output_dir.mkdir(parents=True, exist_ok=True)

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

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            session.begin_stage("set_layout")
            await session.set_layout("speaker")
            session.end_stage("set_layout")

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
            result.end_reason, result.ffmpeg_exit_code = await self._monitor_recording(
                session=session,
                job=job,
                detection_orchestrator=detection_orchestrator,
                ffmpeg_stall_timeout_sec=settings.ffmpeg_stall_timeout_sec,
                ffmpeg_stall_grace_sec=settings.ffmpeg_stall_grace_sec,
            )
            session.end_stage("monitor_recording")

            self._update_status(JobStatus.FINALIZING)
            session.begin_stage("finalize_capture")
            result.recording_info = await session.finalize_capture()
            session.end_stage("finalize_capture")
            result.recording_stopped_at = utc_now()

            result.status = JobStatus.SUCCEEDED
            result.end_time = utc_now()
            result.runtime_summary = session.build_runtime_summary(
                end_reason=result.end_reason,
                recording_info=result.recording_info,
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
            logger.error(f"Recording failed: {e} (stage={result.failure_stage}, diagnostics={session.diagnostics_dir})")
            result.diagnostic_data = await session.collect_diagnostics(
                error_code=result.error_code,
                error_message=result.error_message,
                runtime_summary=result.runtime_summary,
            )

        finally:
            if detection_orchestrator is not None and detection_orchestrator.detection_log:
                try:
                    from database.models import DetectionLog, get_session_local
                    from database.models import RecordingJob as DBJob

                    SessionLocal = get_session_local()
                    db_session = SessionLocal()
                    try:
                        db_job = db_session.query(DBJob).filter(DBJob.job_id == job.job_id).first()
                        if db_job:
                            for log_entry in detection_orchestrator.detection_log:
                                detection_log = DetectionLog(
                                    job_id=db_job.id,
                                    detector_type=log_entry.detector_type.value,
                                    detected=log_entry.detected,
                                    confidence=log_entry.confidence,
                                    reason=log_entry.reason,
                                    attempt_no=job.attempt_no,
                                    triggered_at=log_entry.timestamp,
                                )
                                db_session.add(detection_log)
                            db_session.commit()
                            logger.info(f"Saved {len(detection_orchestrator.detection_log)} detection logs")
                    finally:
                        db_session.close()
                except Exception as e:
                    logger.warning(f"Failed to save detection logs: {e}")

            await session.cleanup()
            self._current_job = None

        return result

    async def _monitor_recording(
        self,
        *,
        session: RecordingSession,
        job: RecordingJob,
        detection_orchestrator: DetectionOrchestrator | None,
        ffmpeg_stall_timeout_sec: int,
        ffmpeg_stall_grace_sec: int,
    ) -> tuple[str, int | None]:
        """Monitor the recording loop until it should stop."""
        recording_start = asyncio.get_event_loop().time()
        check_interval = 5
        last_size = 0
        last_growth_time = recording_start

        effective_min_duration = job.min_duration_sec if job.min_duration_sec is not None else job.duration_sec
        if effective_min_duration > job.duration_sec:
            effective_min_duration = job.duration_sec
        logger.info(f"Recording with min_duration={effective_min_duration}s, max_duration={job.duration_sec}s")

        while True:
            now = asyncio.get_event_loop().time()
            elapsed = now - recording_start

            if self._finish_requested:
                logger.info("Finish requested, stopping recording early")
                return "completed", session.process_returncode()

            if session.process_returncode() is not None:
                raise RuntimeError(f"FFmpeg exited early (code {session.process_returncode()})")

            if ffmpeg_stall_timeout_sec > 0 and elapsed >= ffmpeg_stall_grace_sec and session.output_file.exists():
                try:
                    current_size = session.output_file.stat().st_size
                except OSError:
                    current_size = last_size

                if current_size > last_size:
                    last_size = current_size
                    last_growth_time = now
                elif (now - last_growth_time) >= ffmpeg_stall_timeout_sec:
                    raise RuntimeError(f"FFmpeg output stalled for {ffmpeg_stall_timeout_sec}s")

            if elapsed >= job.duration_sec:
                logger.info(f"Duration reached ({job.duration_sec}s)")
                return "completed", session.process_returncode()

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            if elapsed >= effective_min_duration:
                if detection_orchestrator:
                    should_end, results = await detection_orchestrator.check_all(session.page)
                    if should_end:
                        triggered = [r for r in results if r.detected]
                        reasons = ", ".join(r.reason for r in triggered[:2])
                        logger.info(f"Meeting ended detected after min_duration: {reasons}")
                        return "auto_detected", session.process_returncode()
                elif await session.detect_meeting_end("monitor_recording"):
                    logger.info("Meeting ended")
                    return "auto_detected", session.process_returncode()
            else:
                if int(elapsed) % 60 == 0 and int(elapsed) > 0:
                    remaining_protection = effective_min_duration - elapsed
                    logger.debug(f"Min duration protection: {remaining_protection:.0f}s remaining")

            if int(elapsed) % 60 == 0 and int(elapsed) > 0:
                remaining = job.duration_sec - elapsed
                logger.info(f"Recording in progress... {elapsed:.0f}s elapsed, {remaining:.0f}s remaining")

            await asyncio.sleep(check_interval)


_worker_instance: RecordingWorker | None = None


def get_worker() -> RecordingWorker:
    """Get the global worker instance."""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = RecordingWorker()
    return _worker_instance
