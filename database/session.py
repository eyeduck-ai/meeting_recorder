import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_settings
from database.base import Base
from database.migrations import run_schema_migrations

if TYPE_CHECKING:
    from database.models import RecordingJob
    from recording.job_types import RecordingResult


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Get or create the database engine lazily."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
        )
    return _engine


def get_session_local() -> sessionmaker[Session]:
    """Get or create the SQLAlchemy session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def init_db() -> None:
    """Initialize database tables and apply idempotent compatibility migrations."""
    import database.models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for getting a database session."""
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Context manager for database sessions."""
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class JobRepository:
    """Repository for RecordingJob operations."""

    def __init__(self, session: Session):
        self.session = session

    def create(self, **kwargs) -> "RecordingJob":
        """Create a new job."""
        from database.models import RecordingJob

        job = RecordingJob(**kwargs)
        self.session.add(job)
        self.session.flush()
        return job

    def get_by_job_id(self, job_id: str) -> "RecordingJob | None":
        """Get job by job_id."""
        from database.models import RecordingJob

        return self.session.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()

    def get_all(self, limit: int = 100, offset: int = 0) -> list["RecordingJob"]:
        """Get all jobs with pagination."""
        from database.models import RecordingJob

        return (
            self.session.query(RecordingJob).order_by(RecordingJob.created_at.desc()).offset(offset).limit(limit).all()
        )

    def get_by_status(self, status: str) -> list["RecordingJob"]:
        """Get jobs by status."""
        from database.models import RecordingJob

        return self.session.query(RecordingJob).filter(RecordingJob.status == status).all()

    def update_status(self, job_id: str, status: str, **kwargs) -> bool:
        """Update job status and optional fields."""
        job = self.get_by_job_id(job_id)
        if not job:
            return False

        job.status = status
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)

        self.session.flush()
        return True

    def delete(self, job_id: str) -> bool:
        """Delete a job."""
        job = self.get_by_job_id(job_id)
        if not job:
            return False

        self.session.delete(job)
        self.session.flush()
        return True


def build_result_update_fields(result: "RecordingResult") -> dict:
    """Build database update fields from a RecordingResult.

    This extracts common field mapping logic used after recording completes.

    Args:
        result: The RecordingResult from a recording job

    Returns:
        Dictionary of fields to update on the database record
    """

    def result_attr(name: str, default=None):
        if type(result).__module__.startswith("unittest.mock") and name not in vars(result):
            return default
        return getattr(result, name, default)

    fields = {
        "completed_at": result_attr("end_time"),
        "attempt_no": result_attr("attempt_no", 1),
        "error_code": result_attr("error_code"),
        "error_message": result_attr("error_message"),
        "failure_stage": result_attr("failure_stage"),
        "last_ffmpeg_exit_code": result_attr("ffmpeg_exit_code"),
    }

    joined_at = result_attr("joined_at")
    if joined_at:
        fields["joined_at"] = joined_at

    recording_started_at = result_attr("recording_started_at")
    if recording_started_at:
        fields["recording_started_at"] = recording_started_at

    recording_stopped_at = result_attr("recording_stopped_at")
    if recording_stopped_at:
        fields["recording_stopped_at"] = recording_stopped_at

    recording_info = result_attr("recording_info")
    if recording_info:
        output_override = result_attr("output_path")
        if not isinstance(output_override, str | os.PathLike):
            output_override = None
        raw_output_override = result_attr("raw_output_path")
        if not isinstance(raw_output_override, str | os.PathLike):
            raw_output_override = None
        preferred_output_path = output_override or recording_info.output_path
        fields["output_path"] = str(preferred_output_path)
        fields["raw_output_path"] = str(raw_output_override or recording_info.output_path)
        fields["file_size"] = recording_info.file_size
        fields["duration_actual_sec"] = recording_info.duration_sec

    trimmed_output_path = result_attr("trimmed_output_path")
    if trimmed_output_path:
        fields["trimmed_output_path"] = str(trimmed_output_path)

    for attr in (
        "trim_start_sec",
        "trim_end_sec",
        "trim_status",
        "trim_reason",
        "dynamic_extension_stop_reason",
    ):
        value = result_attr(attr)
        if value is not None:
            fields[attr] = value

    diagnostic_data = result_attr("diagnostic_data")
    if diagnostic_data:
        fields["diagnostic_dir"] = str(diagnostic_data.output_dir) if diagnostic_data.output_dir else None
        fields["has_screenshot"] = diagnostic_data.screenshot_path is not None
        fields["has_html_dump"] = diagnostic_data.html_path is not None
        fields["has_console_log"] = diagnostic_data.console_log_path is not None

    end_reason = result_attr("end_reason")
    if end_reason:
        fields["end_reason"] = end_reason

    runtime_summary = result_attr("runtime_summary")
    if isinstance(runtime_summary, dict):
        fields["runtime_summary_json"] = json.dumps(runtime_summary, ensure_ascii=False)

    return fields
