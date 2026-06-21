import json
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base
from utils.timezone import utc_now


class ScheduleType(StrEnum):
    """Schedule type."""

    ONCE = "once"
    CRON = "cron"


class JobStatus(StrEnum):
    """Recording job status."""

    QUEUED = "queued"
    STARTING = "starting"
    JOINING = "joining"
    WAITING_LOBBY = "waiting_lobby"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    UPLOADING = "uploading"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ErrorCode(StrEnum):
    """Standard error codes for recording failures."""

    # Join errors
    JOIN_TIMEOUT = "JOIN_TIMEOUT"
    JOIN_FAILED = "JOIN_FAILED"
    INVALID_URL = "INVALID_URL"
    MEETING_NOT_FOUND = "MEETING_NOT_FOUND"
    PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
    PASSWORD_INCORRECT = "PASSWORD_INCORRECT"

    # Lobby errors
    LOBBY_TIMEOUT = "LOBBY_TIMEOUT"
    LOBBY_REJECTED = "LOBBY_REJECTED"
    NEVER_JOINED = "NEVER_JOINED"  # Never successfully joined meeting

    # Recording errors
    RECORDING_START_FAILED = "RECORDING_START_FAILED"
    RECORDING_INTERRUPTED = "RECORDING_INTERRUPTED"
    FFMPEG_ERROR = "FFMPEG_ERROR"

    # Meeting errors
    MEETING_ENDED = "MEETING_ENDED"
    KICKED_FROM_MEETING = "KICKED_FROM_MEETING"
    CONNECTION_LOST = "CONNECTION_LOST"

    # System errors
    BROWSER_CRASHED = "BROWSER_CRASHED"
    VIRTUAL_ENV_ERROR = "VIRTUAL_ENV_ERROR"
    DISK_FULL = "DISK_FULL"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # User actions
    CANCELED = "CANCELED"


class Meeting(Base):
    """Meeting configuration model."""

    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="jitsi")

    # Meeting details
    site_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meeting_code: Mapped[str] = mapped_column(String(255), nullable=False)
    join_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meeting_password_plaintext: Mapped[str | None] = mapped_column("password_encrypted", String(512), nullable=True)

    # Default identity
    default_display_name: Mapped[str] = mapped_column(String(255), default="Recorder Bot")
    default_guest_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_guest_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    schedules: Mapped[list["Schedule"]] = relationship(
        "Schedule", back_populates="meeting", cascade="all, delete-orphan"
    )

    @property
    def has_password(self) -> bool:
        """Return whether the meeting has a stored password."""
        return bool(self.meeting_password_plaintext)


class Schedule(Base):
    """Schedule model for recurring/one-time recordings."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"), nullable=False)

    # Schedule timing
    schedule_type: Mapped[str] = mapped_column(String(32), default=ScheduleType.ONCE.value)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=4200)
    duration_mode: Mapped[str] = mapped_column(String(32), default="fixed")  # legacy; always fixed
    cron_expression: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Recording settings
    lobby_wait_sec: Mapped[int] = mapped_column(Integer, default=900)
    layout_preset: Mapped[str] = mapped_column(String(32), default="speaker")
    resolution_w: Mapped[int] = mapped_column(Integer, default=1920)
    resolution_h: Mapped[int] = mapped_column(Integer, default=1080)

    # Identity overrides (per-schedule)
    override_meeting_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_guest_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_guest_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # YouTube settings (Phase 4)
    youtube_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_privacy: Mapped[str] = mapped_column(String(32), default="unlisted")

    # Advanced timing settings
    early_join_sec: Mapped[int] = mapped_column(Integer, default=30)  # Join meeting early
    min_duration_sec: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Min recording time (None = use duration_sec)
    stillness_timeout_sec: Mapped[int] = mapped_column(Integer, default=180)  # legacy provider auto-detect setting

    # Detection settings
    auto_detect_mode: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # legacy provider auto-detect setting
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)  # legacy provider auto-detect setting

    # Smart recording boundary overrides (None = inherit global default)
    smart_trim_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dynamic_extension_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dynamic_extension_idle_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dynamic_extension_max_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="schedules")

    def get_effective_meeting_code(self) -> str:
        """Get meeting code (override or default from meeting)."""
        return self.override_meeting_code or self.meeting.meeting_code

    def get_effective_display_name(self) -> str:
        """Get display name (override or default from meeting)."""
        return self.override_display_name or self.meeting.default_display_name


class RecordingJob(Base):
    """Recording job model."""

    __tablename__ = "recording_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    schedule_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("schedules.id"), nullable=True)

    # Relationships
    schedule: Mapped["Schedule"] = relationship("Schedule")

    # Job configuration
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    meeting_code: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meeting_password_plaintext: Mapped[str | None] = mapped_column("password_hash", String(255), nullable=True)

    # Timing
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    lobby_wait_sec: Mapped[int] = mapped_column(Integer, default=900)

    # Status
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_ffmpeg_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recording_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recording_stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    youtube_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Recording output
    output_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_output_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    trimmed_output_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_actual_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_recording_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    local_recording_cleanup_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trim_start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    trim_end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    trim_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trim_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dynamic_extension_stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Diagnostics
    diagnostic_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    has_screenshot: Mapped[bool] = mapped_column(Boolean, default=False)
    has_html_dump: Mapped[bool] = mapped_column(Boolean, default=False)
    has_console_log: Mapped[bool] = mapped_column(Boolean, default=False)

    # YouTube (Phase 4)
    youtube_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Telegram notification tracking (Phase 12)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # End reason tracking (for catch-up logic)
    end_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # 'completed' | 'auto_detected' | 'canceled' | 'failed' | 'timeout'

    @property
    def runtime_summary(self) -> dict | None:
        """Return the parsed runtime summary payload."""
        if not self.runtime_summary_json:
            return None
        try:
            return json.loads(self.runtime_summary_json)
        except json.JSONDecodeError:
            return None


class TelegramUser(Base):
    """Telegram user model for bot notifications and commands."""

    __tablename__ = "telegram_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Approval status
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Notification preferences
    notify_on_start: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_failure: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_upload: Mapped[bool] = mapped_column(Boolean, default=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def display_name(self) -> str:
        """Get display name for the user."""
        if self.username:
            return f"@{self.username}"
        elif self.first_name:
            return self.first_name + (f" {self.last_name}" if self.last_name else "")
        return f"User {self.chat_id}"


class AppSettings(Base):
    """Application settings stored in database (user-configurable)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class DetectionLog(Base):
    """Log of detection events for analysis and tuning."""

    __tablename__ = "detection_logs"
    __table_args__ = (
        Index("ix_detection_logs_triggered_at", "triggered_at"),
        Index("ix_detection_logs_job_triggered_at", "job_id", "triggered_at"),
        Index("ix_detection_logs_type_detected_triggered_at", "detector_type", "detected", "triggered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("recording_jobs.id"), nullable=False)
    detector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detected: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    was_accurate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # For manual review
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
