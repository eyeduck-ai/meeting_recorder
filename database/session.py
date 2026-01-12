from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from database.models import get_session_local

if TYPE_CHECKING:
    from database.models import RecordingJob
    from recording.worker import RecordingResult


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
    fields = {
        "completed_at": result.end_time,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }

    if result.joined_at:
        fields["joined_at"] = result.joined_at

    if result.recording_started_at:
        fields["recording_started_at"] = result.recording_started_at

    if result.recording_info:
        fields["output_path"] = str(result.recording_info.output_path)
        fields["file_size"] = result.recording_info.file_size
        fields["duration_actual_sec"] = result.recording_info.duration_sec

    if result.diagnostic_data:
        fields["diagnostic_dir"] = str(result.diagnostic_data.output_dir) if result.diagnostic_data.output_dir else None
        fields["has_screenshot"] = result.diagnostic_data.screenshot_path is not None
        fields["has_html_dump"] = result.diagnostic_data.html_path is not None
        fields["has_console_log"] = result.diagnostic_data.console_log_path is not None

    return fields
