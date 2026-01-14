from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from config.settings import get_settings


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class ProviderType(str, Enum):
    """Supported meeting providers."""

    JITSI = "jitsi"
    WEBEX = "webex"


class ScheduleType(str, Enum):
    """Schedule type."""

    ONCE = "once"
    CRON = "cron"


class DurationMode(str, Enum):
    """Duration mode for recordings."""

    FIXED = "fixed"  # Use fixed duration_sec
    AUTO = "auto"  # Auto-detect meeting end


class JobStatus(str, Enum):
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


class ErrorCode(str, Enum):
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
    provider: Mapped[str] = mapped_column(String(32), default=ProviderType.JITSI.value)

    # Meeting details
    site_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meeting_code: Mapped[str] = mapped_column(String(255), nullable=False)
    join_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Default identity
    default_display_name: Mapped[str] = mapped_column(String(255), default="Recorder Bot")
    default_guest_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_guest_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    schedules: Mapped[list["Schedule"]] = relationship(
        "Schedule", back_populates="meeting", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "site_base_url": self.site_base_url,
            "meeting_code": self.meeting_code,
            "join_url": self.join_url,
            "default_display_name": self.default_display_name,
            "default_guest_name": self.default_guest_name,
            "default_guest_email": self.default_guest_email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Schedule(Base):
    """Schedule model for recurring/one-time recordings."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"), nullable=False)

    # Schedule timing
    schedule_type: Mapped[str] = mapped_column(String(32), default=ScheduleType.ONCE.value)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=4200)
    duration_mode: Mapped[str] = mapped_column(String(32), default=DurationMode.FIXED.value)  # fixed or auto
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

    # Detection settings
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)  # Log only, don't stop

    # Status
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="schedules")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "meeting_id": self.meeting_id,
            "schedule_type": self.schedule_type,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "duration_sec": self.duration_sec,
            "duration_mode": self.duration_mode,
            "cron_expression": self.cron_expression,
            "lobby_wait_sec": self.lobby_wait_sec,
            "layout_preset": self.layout_preset,
            "resolution_w": self.resolution_w,
            "resolution_h": self.resolution_h,
            "override_meeting_code": self.override_meeting_code,
            "override_display_name": self.override_display_name,
            "youtube_enabled": self.youtube_enabled,
            "youtube_privacy": self.youtube_privacy,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

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

    # Job configuration
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    meeting_code: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timing
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    lobby_wait_sec: Mapped[int] = mapped_column(Integer, default=900)

    # Status
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recording_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Recording output
    output_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_actual_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Diagnostics
    diagnostic_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    has_screenshot: Mapped[bool] = mapped_column(Boolean, default=False)
    has_html_dump: Mapped[bool] = mapped_column(Boolean, default=False)
    has_console_log: Mapped[bool] = mapped_column(Boolean, default=False)

    # YouTube (Phase 4)
    youtube_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "job_id": self.job_id,
            "provider": self.provider,
            "meeting_code": self.meeting_code,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "duration_sec": self.duration_sec,
            "lobby_wait_sec": self.lobby_wait_sec,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "recording_started_at": self.recording_started_at.isoformat() if self.recording_started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "output_path": self.output_path,
            "file_size": self.file_size,
            "duration_actual_sec": self.duration_actual_sec,
            "diagnostic_dir": self.diagnostic_dir,
            "has_screenshot": self.has_screenshot,
            "has_html_dump": self.has_html_dump,
            "has_console_log": self.has_console_log,
            "youtube_enabled": self.youtube_enabled,
            "youtube_video_id": self.youtube_video_id,
        }


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "notify_on_start": self.notify_on_start,
            "notify_on_complete": self.notify_on_complete,
            "notify_on_failure": self.notify_on_failure,
            "notify_on_upload": self.notify_on_upload,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_interaction_at": self.last_interaction_at.isoformat() if self.last_interaction_at else None,
        }

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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DetectionLog(Base):
    """Log of detection events for analysis and tuning."""

    __tablename__ = "detection_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("recording_jobs.id"), nullable=False)
    detector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detected: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_accurate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # For manual review
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "detector_type": self.detector_type,
            "detected": self.detected,
            "confidence": self.confidence,
            "reason": self.reason,
            "was_accurate": self.was_accurate,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
        }


# Database engine and session
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create database engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
        )
    return _engine


def get_session_local():
    """Get session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=get_engine())


def get_db():
    """Dependency for getting database session."""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
