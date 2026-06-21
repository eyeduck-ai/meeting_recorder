"""Service layer for immediate recording jobs."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from database.models import RecordingJob
from database.session import JobRepository
from services.errors import ConflictError, ServiceError

BUSY_DETAIL = "Recording could not be queued."


@dataclass(frozen=True)
class ImmediateRecordingData:
    """Fields required to start an immediate recording."""

    provider: str
    meeting_code: str
    display_name: str
    duration_sec: int
    base_url: str | None = None
    password: str | None = None
    lobby_wait_sec: int | None = None


class JobService:
    """Coordinate immediate recording job creation."""

    def __init__(self, *, job_runner=None):
        self._job_runner = job_runner

    async def start_immediate_recording(self, db: Session, data: ImmediateRecordingData) -> RecordingJob:
        """Start an immediate recording and return the persisted DB job."""
        runner = self._get_job_runner()
        job_id = await runner.run_immediate(
            provider=data.provider,
            meeting_code=data.meeting_code,
            display_name=data.display_name,
            duration_sec=data.duration_sec,
            base_url=data.base_url,
            password=data.password,
            lobby_wait_sec=data.lobby_wait_sec,
        )
        if not job_id:
            raise ConflictError(BUSY_DETAIL)

        db_job = JobRepository(db).get_by_job_id(job_id)
        if not db_job:
            raise ServiceError("Failed to create recording job")
        return db_job

    def _get_job_runner(self):
        if self._job_runner is None:
            from scheduling.job_runner import get_job_runner

            return get_job_runner()
        return self._job_runner
