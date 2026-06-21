"""Shared lifecycle actions for recording jobs."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from database.models import ErrorCode, JobStatus, RecordingJob, Schedule
from database.session import JobRepository
from services.errors import NotFoundError, ValidationError
from utils.timezone import utc_now

ACTIVE_RECORDING_STATUSES = {
    JobStatus.STARTING.value,
    JobStatus.JOINING.value,
    JobStatus.WAITING_LOBBY.value,
    JobStatus.RECORDING.value,
    JobStatus.FINALIZING.value,
}
TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELED.value,
}


def job_status_value(job: RecordingJob) -> str:
    return job.status.value if hasattr(job.status, "value") else job.status


@dataclass(frozen=True)
class JobActionResult:
    """Result of a lifecycle action."""

    message: str
    job_id: str | None = None
    count: int | None = None


class JobActionService:
    """Centralized job lifecycle state machine for API and Web UI routes."""

    def __init__(self, *, worker, job_runner):
        self._worker = worker
        self._job_runner = job_runner

    def stop_job(self, db: Session, job_id: str) -> JobActionResult:
        job = self._get_job(db, job_id)
        status = job_status_value(job)
        if status in TERMINAL_JOB_STATUSES:
            raise ValidationError(f"Job is already in terminal state: {status}")
        if status == JobStatus.UPLOADING.value:
            raise ValidationError("Uploading jobs cannot be stopped")

        if status == JobStatus.QUEUED.value:
            cancel_queued_job_for_action = getattr(self._job_runner, "cancel_queued_job_for_action", None)
            if not cancel_queued_job_for_action:
                raise ValidationError("Job is queued but not cancelable in this process")

            cancel_result = cancel_queued_job_for_action(job_id)
            removed = bool(getattr(cancel_result, "removed", cancel_result))
            source = getattr(cancel_result, "source", None)

            if removed:
                is_retry_waiting = source == "retry_waiting"
                repo = JobRepository(db)
                repo.update_status(
                    job_id,
                    JobStatus.CANCELED.value,
                    error_code=ErrorCode.CANCELED.value,
                    error_message="Canceled while waiting to retry" if is_retry_waiting else "Canceled while queued",
                    completed_at=utc_now(),
                    end_reason="canceled",
                )
                db.commit()
                message = "Retry waiting job canceled" if is_retry_waiting else "Queued job canceled"
                return JobActionResult(message=message, job_id=job_id)
            raise ValidationError("Job is queued but not cancelable in this process")

        if status in ACTIVE_RECORDING_STATUSES:
            if self._worker.is_job_active(job_id) and self._worker.request_cancel(job_id):
                return JobActionResult(message="Cancellation requested", job_id=job_id)
            raise ValidationError("Job is not currently running")

        raise ValidationError(f"Job cannot be stopped from state: {status}")

    def finish_job(self, db: Session, job_id: str) -> JobActionResult:
        job = self._get_job(db, job_id)
        status = job_status_value(job)
        if status in TERMINAL_JOB_STATUSES:
            raise ValidationError(f"Job is already in terminal state: {status}")
        if status == JobStatus.QUEUED.value:
            raise ValidationError("Queued jobs cannot be finished")
        if status == JobStatus.UPLOADING.value:
            raise ValidationError("Uploading jobs cannot be finished")

        if status in ACTIVE_RECORDING_STATUSES and self._worker.is_job_active(job_id):
            if self._worker.request_finish(job_id):
                return JobActionResult(message="Finish requested", job_id=job_id)
            raise ValidationError("Could not request finish")

        raise ValidationError("Job is not currently running")

    def delete_job(self, db: Session, job_id: str) -> JobActionResult:
        job = self._get_job(db, job_id)
        status = job_status_value(job)
        if status not in TERMINAL_JOB_STATUSES:
            raise ValidationError("Only terminal jobs can be deleted")

        db.delete(job)
        db.commit()
        return JobActionResult(message="Job deleted", job_id=job_id)

    def delete_terminal_jobs(self, db: Session) -> JobActionResult:
        jobs = db.query(RecordingJob).filter(RecordingJob.status.in_(TERMINAL_JOB_STATUSES)).all()
        count = len(jobs)
        for job in jobs:
            db.delete(job)
        db.commit()
        return JobActionResult(message="Terminal jobs deleted", count=count)

    def cancel_queued_schedule(self, db: Session, schedule_id: int) -> JobActionResult:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            raise NotFoundError("Schedule not found")

        cancel_queued_schedule = getattr(self._job_runner, "cancel_queued_schedule", None)
        if not cancel_queued_schedule or not cancel_queued_schedule(schedule_id):
            raise ValidationError("Schedule is not queued")

        return JobActionResult(message="Queued schedule run canceled")

    def _get_job(self, db: Session, job_id: str) -> RecordingJob:
        job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
        if not job:
            raise NotFoundError("Job not found")
        return job
