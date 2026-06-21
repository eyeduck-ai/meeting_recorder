"""Recording job/result data transfer objects."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config.settings import get_settings
from database.models import JobStatus
from recording.activity import ActivityConfig
from recording.ffmpeg_pipeline import RecordingInfo
from services.runtime_config import RuntimeRecordingConfig, get_runtime_config_service
from utils.timezone import utc_now


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
    hard_deadline_at: datetime | None = None
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
    ) -> RecordingJob:
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
            hard_deadline_at=kwargs.get("hard_deadline_at"),
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
            smart_trim_enabled=runtime_config.smart_trim_enabled,
            dynamic_extension_enabled=runtime_config.dynamic_extension_enabled,
            dynamic_extension_idle_sec=runtime_config.dynamic_extension_idle_sec,
            dynamic_extension_max_sec=runtime_config.dynamic_extension_max_sec,
            activity_config=runtime_config.activity_config,
        )
