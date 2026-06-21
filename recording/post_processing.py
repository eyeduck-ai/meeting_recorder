"""Post-recording processing coordination."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from config.settings import get_settings
from database.models import DetectionLog, JobStatus
from database.models import RecordingJob as RecordingJobModel
from database.session import JobRepository, build_result_update_fields, get_session_local
from recording.activity import RecordingActivityAnalyzer, trim_recording
from recording.job_types import RecordingJob, RecordingResult
from scheduling.upload_runner import UploadRequest
from services.storage_maintenance import canonicalize_recording_file
from telegram_bot.notifications import notify_recording_completed
from utils.timezone import to_local, utc_now

logger = logging.getLogger(__name__)


class ActivityAnalysisLimiter:
    """Limit completed-file activity analysis and trim subprocess concurrency."""

    def __init__(self, *, max_parallel: int | None = None, settings_provider: Callable = get_settings) -> None:
        self._max_parallel = max_parallel
        self._settings_provider = settings_provider
        self._semaphore: asyncio.Semaphore | None = None
        self._limit: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @contextlib.asynccontextmanager
    async def slot(self):
        """Acquire one completed-file post-processing slot."""
        semaphore = self._get_semaphore()
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        limit = self._current_limit()
        if self._semaphore is None or self._limit != limit or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(limit)
            self._limit = limit
            self._loop = loop
        return self._semaphore

    def _current_limit(self) -> int:
        if self._max_parallel is not None:
            return max(1, int(self._max_parallel))
        settings = self._settings_provider()
        return max(1, int(getattr(settings, "max_parallel_activity_analyses", 1) or 1))


@dataclass(frozen=True)
class RecordingPostProcessingRequest:
    """A successful raw capture that still needs completed-file processing."""

    job: RecordingJob
    result: RecordingResult
    youtube_enabled: bool
    youtube_privacy: str
    meeting_name: str | None


class RecordingPostProcessor:
    """Finalize successful raw captures without occupying recording capacity."""

    def __init__(
        self,
        *,
        activity_analysis_limiter: ActivityAnalysisLimiter | None = None,
        analyzer_factory: Callable = RecordingActivityAnalyzer,
        trim_func: Callable = trim_recording,
        canonicalizer: Callable = canonicalize_recording_file,
        session_factory: Callable | None = None,
        completed_notifier: Callable = notify_recording_completed,
    ) -> None:
        self._activity_analysis_limiter = activity_analysis_limiter or ActivityAnalysisLimiter()
        self._analyzer_factory = analyzer_factory
        self._trim_func = trim_func
        self._canonicalizer = canonicalizer
        self._session_factory = session_factory or get_session_local
        self._completed_notifier = completed_notifier

    async def run(self, request: RecordingPostProcessingRequest) -> UploadRequest | None:
        """Run best-effort completed-file processing and mark the job terminal."""
        result = request.result
        if result.status != JobStatus.SUCCEEDED:
            return None

        try:
            await self.apply_smart_trim(
                job=request.job,
                result=result,
                diagnostics_dir=request.job.diagnostics_dir or request.job.output_dir,
            )
        except Exception as exc:
            logger.exception("Smart trim post-processing failed for job %s", request.job.job_id)
            self._mark_trim_failed(result, f"smart trim post-processing failed; raw recording retained: {exc}")

        await self._canonicalize_successful_recording(request.job, result)
        result.status = JobStatus.SUCCEEDED
        result.end_time = utc_now()
        self._update_runtime_summary(result)
        self._write_runtime_summary(request.job, result)
        await self._save_activity_detection_log(request.job, result)
        self._persist_terminal_success(request)
        await self._notify_completed(request.job.job_id)
        return self._build_upload_request(request)

    async def complete_with_raw_recording(
        self,
        request: RecordingPostProcessingRequest,
        *,
        error_message: str,
    ) -> None:
        """Settle an interrupted post-processing task as raw-recording success."""
        result = request.result
        self._ensure_raw_output_defaults(result)
        if result.trim_status is None:
            result.trim_status = "failed"
            result.trim_reason = error_message
        result.status = JobStatus.SUCCEEDED
        result.end_time = utc_now()
        result.error_message = error_message
        self._update_runtime_summary(result)
        self._write_runtime_summary(request.job, result)
        self._persist_terminal_success(request)

    async def apply_smart_trim(
        self,
        *,
        job: RecordingJob,
        result: RecordingResult,
        diagnostics_dir: Path,
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

        async with self._activity_analysis_limiter.slot():
            analyzer = self._analyzer_factory(job.activity_config)
            decision = await analyzer.analyze(raw_path)
            result.trim_start_sec = decision.trim_start_sec
            result.trim_end_sec = decision.trim_end_sec
            result.trim_status = decision.status
            result.trim_reason = decision.reason
            result.trim_diagnostics = dict(decision.diagnostics)

            if not decision.should_trim or decision.trim_end_sec is None:
                return

            trimmed_path = raw_path.with_name(f"{raw_path.stem}.trimmed{raw_path.suffix}")
            trimmed_info = await self._trim_func(
                input_path=raw_path,
                output_path=trimmed_path,
                trim_start_sec=decision.trim_start_sec,
                trim_end_sec=decision.trim_end_sec,
                log_path=diagnostics_dir / "trim.log",
            )
        if not trimmed_info:
            result.trim_status = "failed"
            result.trim_reason = "trim command failed; raw recording retained"
            result.trimmed_output_path = None
            result.output_path = raw_path
            result.recording_info = raw_info
            return

        expected_duration_sec = max(0.0, decision.trim_end_sec - decision.trim_start_sec)
        result.trim_diagnostics["trim_output_expected_duration_sec"] = round(expected_duration_sec, 3)
        result.trim_diagnostics["trim_output_actual_duration_sec"] = round(trimmed_info.duration_sec, 3)
        trimmed_info.start_time = raw_info.start_time + timedelta(seconds=decision.trim_start_sec)
        trimmed_info.end_time = trimmed_info.start_time + timedelta(seconds=trimmed_info.duration_sec)
        result.trimmed_output_path = trimmed_path
        result.output_path = trimmed_path
        result.recording_info = trimmed_info

    def _persist_terminal_success(self, request: RecordingPostProcessingRequest) -> None:
        SessionLocal = self._session_factory()
        session = SessionLocal()
        try:
            repo = JobRepository(session)
            update_fields = build_result_update_fields(request.result)
            update_fields["attempt_no"] = request.result.attempt_no
            update_fields["retry_count"] = max(0, request.result.attempt_no - 1)
            update_fields["youtube_enabled"] = request.youtube_enabled
            repo.update_status(request.job.job_id, JobStatus.SUCCEEDED.value, **update_fields)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _notify_completed(self, job_id: str) -> None:
        SessionLocal = self._session_factory()
        session = SessionLocal()
        try:
            db_job = JobRepository(session).get_by_job_id(job_id)
            if not db_job:
                return
            await self._completed_notifier(db_job)
        except Exception as exc:
            logger.warning("Failed to send recording completed notification for job %s: %s", job_id, exc)
        finally:
            session.close()

    def _build_upload_request(self, request: RecordingPostProcessingRequest) -> UploadRequest | None:
        result = request.result
        output_path = result.output_path or (result.recording_info.output_path if result.recording_info else None)
        if not request.youtube_enabled or not output_path or not output_path.exists():
            return None

        cleanup_path = None
        if result.trimmed_output_path and output_path == result.trimmed_output_path:
            cleanup_path = result.trimmed_output_path
        recording_time = result.recording_started_at or result.start_time or utc_now()
        local_time = to_local(recording_time)
        time_str = local_time.strftime("%Y%m%d_%H%M")
        title_parts = [time_str]
        if request.meeting_name:
            title_parts.append(request.meeting_name)
        title_parts.append(request.job.meeting_code)
        return UploadRequest(
            job_id=request.job.job_id,
            video_path=output_path,
            title=" - ".join(title_parts),
            privacy=request.youtube_privacy,
            meeting_name=request.meeting_name,
            raw_video_path=result.raw_output_path,
            cleanup_video_path_after_success=cleanup_path,
        )

    async def _canonicalize_successful_recording(self, job: RecordingJob, result: RecordingResult) -> None:
        """Best-effort conversion to the local MP4 canonical file."""
        recording_info = result.recording_info
        if not recording_info or recording_info.output_path.suffix.lower() != ".mkv":
            return

        original_output_path = recording_info.output_path
        remux_log = job.diagnostics_dir / "remux.log" if job.diagnostics_dir else None
        transcode_log = job.diagnostics_dir / "transcode.log" if job.diagnostics_dir else None
        try:
            canonical = await self._canonicalizer(
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

    def _update_runtime_summary(self, result: RecordingResult) -> None:
        if not isinstance(result.runtime_summary, dict):
            result.runtime_summary = {}
        result.runtime_summary["trim"] = self.build_trim_summary(result)
        if result.recording_info:
            result.runtime_summary["recording_info"] = {
                "output_path": str(result.recording_info.output_path),
                "file_size": result.recording_info.file_size,
                "duration_sec": result.recording_info.duration_sec,
                "start_time": result.recording_info.start_time.isoformat(),
                "end_time": result.recording_info.end_time.isoformat(),
            }

    def _write_runtime_summary(self, job: RecordingJob, result: RecordingResult) -> None:
        if not isinstance(result.runtime_summary, dict) or not job.diagnostics_dir:
            return
        try:
            job.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            (job.diagnostics_dir / "runtime.json").write_text(
                json.dumps(result.runtime_summary, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to write runtime summary for job %s: %s", job.job_id, exc)

    async def _save_activity_detection_log(self, job: RecordingJob, result: RecordingResult) -> None:
        if result.trim_status is None:
            return
        SessionLocal = self._session_factory()
        db_session = SessionLocal()
        try:
            db_job = db_session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).first()
            if not db_job:
                return
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
        except Exception as exc:
            db_session.rollback()
            logger.warning("Failed to save post-processing detection log for job %s: %s", job.job_id, exc)
        finally:
            db_session.close()

    def _ensure_raw_output_defaults(self, result: RecordingResult) -> None:
        if not result.recording_info:
            return
        if result.raw_output_path is None:
            result.raw_output_path = result.recording_info.output_path
        if result.output_path is None:
            result.output_path = result.recording_info.output_path
        if result.trim_start_sec is None:
            result.trim_start_sec = 0.0
        if result.trim_end_sec is None:
            result.trim_end_sec = result.recording_info.duration_sec

    def _mark_trim_failed(self, result: RecordingResult, message: str) -> None:
        self._ensure_raw_output_defaults(result)
        result.trim_status = "failed"
        result.trim_reason = message
        result.trimmed_output_path = None
        result.error_message = message

    @staticmethod
    def build_trim_summary(result: RecordingResult) -> dict:
        return {
            "raw_output_path": str(result.raw_output_path) if result.raw_output_path else None,
            "trimmed_output_path": str(result.trimmed_output_path) if result.trimmed_output_path else None,
            "trim_start_sec": result.trim_start_sec,
            "trim_end_sec": result.trim_end_sec,
            "trim_status": result.trim_status,
            "trim_reason": result.trim_reason,
            "diagnostics": result.trim_diagnostics,
        }

    @staticmethod
    def _replace_result_path_if_original(result: RecordingResult, attr: str, original_path: Path, canonical_path: Path):
        value = getattr(result, attr, None)
        if RecordingPostProcessor._path_value_matches(value, original_path):
            setattr(result, attr, canonical_path)

    @staticmethod
    def _path_value_matches(value, expected: Path) -> bool:
        if not isinstance(value, str | Path):
            return False
        return Path(value) == expected
