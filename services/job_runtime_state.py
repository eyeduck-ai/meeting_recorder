"""Shared runtime state view for active, queued, and retry-waiting jobs."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from database.models import JobStatus, Schedule
from database.models import RecordingJob as RecordingJobModel
from services.job_actions import ACTIVE_RECORDING_STATUSES, job_status_value


def _coerce_non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _coerce_positive_int(value: Any) -> int | None:
    parsed = _coerce_non_negative_int(value)
    if parsed is None or parsed < 1:
        return None
    return parsed


def active_job_payload(job: RecordingJobModel) -> dict[str, Any]:
    """Return the public active-job payload used by REST and status views."""
    return {
        "job_id": job.job_id,
        "status": job_status_value(job),
        "meeting_code": job.meeting_code,
        "display_name": job.display_name,
        "duration_sec": job.duration_sec,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "recording_started_at": job.recording_started_at.isoformat() if job.recording_started_at else None,
        "detectors": {},
    }


def build_queued_item_payloads(db: Session, queued_items: list) -> list[dict[str, Any]]:
    """Attach DB-backed display fields to FIFO queued run items."""
    queued_job_ids = [item.job_id for item in queued_items if item.job_id]
    queued_schedule_ids = [item.schedule_id for item in queued_items if item.schedule_id]
    queued_jobs_by_id = {}
    queued_schedules_by_id = {}
    if queued_job_ids:
        queued_jobs_by_id = {
            job.job_id: job
            for job in db.query(RecordingJobModel).filter(RecordingJobModel.job_id.in_(queued_job_ids)).all()
        }
    if queued_schedule_ids:
        queued_schedules_by_id = {
            schedule.id: schedule for schedule in db.query(Schedule).filter(Schedule.id.in_(queued_schedule_ids)).all()
        }

    return [_queued_item_payload(item, queued_jobs_by_id, queued_schedules_by_id) for item in queued_items]


def build_retry_waiting_item_payloads(db: Session, retry_items: list) -> list[dict[str, Any]]:
    """Attach DB-backed display fields to delayed retry waiting items."""
    retry_job_ids = [item.job_id for item in retry_items if item.job_id]
    jobs_by_id = {}
    if retry_job_ids:
        jobs_by_id = {
            job.job_id: job
            for job in db.query(RecordingJobModel).filter(RecordingJobModel.job_id.in_(retry_job_ids)).all()
        }

    payloads = []
    for item in retry_items:
        job = jobs_by_id.get(item.job_id)
        payloads.append(
            {
                "job_id": item.job_id,
                "schedule_id": item.schedule_id,
                "status": "retry_waiting",
                "retry_after_sec": item.retry_after_sec,
                "meeting_code": job.meeting_code if job else item.meeting_code,
                "display_name": job.display_name if job else item.display_name,
            }
        )
    return payloads


def _queued_item_payload(
    item,
    jobs_by_id: dict[str, RecordingJobModel],
    schedules_by_id: dict[int, Schedule],
) -> dict[str, Any]:
    payload = {
        "kind": item.kind,
        "queue_position": item.queue_position,
        "job_id": item.job_id,
        "schedule_id": item.schedule_id,
        "status": JobStatus.QUEUED.value,
        "meeting_code": None,
        "display_name": None,
        "manual_trigger": item.manual_trigger,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
    if item.kind == "immediate" and item.job_id:
        job = jobs_by_id.get(item.job_id)
        if job:
            payload["meeting_code"] = job.meeting_code
            payload["display_name"] = job.display_name
    elif item.kind == "schedule" and item.schedule_id:
        schedule = schedules_by_id.get(item.schedule_id)
        if schedule:
            payload["meeting_code"] = schedule.get_effective_meeting_code()
            payload["display_name"] = schedule.get_effective_display_name()
    return payload


@dataclass(frozen=True)
class JobRuntimeSnapshot:
    """Process-local runtime state joined with persisted job/schedule rows."""

    active_jobs: list[RecordingJobModel]
    queued_items: list[dict[str, Any]]
    retry_waiting_items: list[dict[str, Any]]
    queue_length: int
    retry_waiting_count: int
    max_concurrent_recordings: int
    available_slots: int
    active_job_ids: set[str] = field(default_factory=set)
    queued_job_ids: set[str] = field(default_factory=set)
    retry_waiting_job_ids: set[str] = field(default_factory=set)
    queued_positions_by_job_id: dict[str, int] = field(default_factory=dict)
    retry_after_by_job_id: dict[str, int] = field(default_factory=dict)
    queued_schedule_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.active_jobs)

    @property
    def active_count(self) -> int:
        return len(self.active_jobs)

    @property
    def latest_active_job(self) -> RecordingJobModel | None:
        return self.active_jobs[0] if self.active_jobs else None

    @property
    def active_job_payloads(self) -> list[dict[str, Any]]:
        return [active_job_payload(job) for job in self.active_jobs]

    def to_active_response(self) -> dict[str, Any]:
        """Return the backward-compatible /jobs/active response payload."""
        return {
            "active": self.active,
            "active_jobs": self.active_job_payloads,
            "active_count": self.active_count,
            "queued_items": self.queued_items,
            "retry_waiting_items": self.retry_waiting_items,
            "retry_waiting_count": self.retry_waiting_count,
            "queue_length": self.queue_length,
            "max_concurrent_recordings": self.max_concurrent_recordings,
            "available_slots": self.available_slots,
        }


class JobRuntimeStateService:
    """Build a single runtime state snapshot for API, Web UI, and Telegram."""

    def _runner_queue_length(self, runner, queued_items: list[dict[str, Any]]) -> int:
        if hasattr(runner, "queue_length"):
            queue_length = _coerce_non_negative_int(runner.queue_length)
            if queue_length is not None:
                return queue_length
        return len(queued_items)

    def _runner_retry_waiting_count(self, runner, retry_waiting_items: list[dict[str, Any]]) -> int:
        if hasattr(runner, "retry_waiting_count"):
            retry_waiting_count = _coerce_non_negative_int(runner.retry_waiting_count)
            if retry_waiting_count is not None:
                return retry_waiting_count
        return len(retry_waiting_items)

    def _runner_configured_max_concurrent_recordings(self, runner) -> int | None:
        if hasattr(runner, "max_concurrent_recordings"):
            return _coerce_positive_int(runner.max_concurrent_recordings)
        return None

    def _runner_max_concurrent_recordings(self, runner) -> int:
        return self._runner_configured_max_concurrent_recordings(runner) or 1

    def _runner_available_slots(self, runner, *, max_concurrent_recordings: int, active_count: int) -> int:
        if hasattr(runner, "available_slots"):
            available_slots = _coerce_non_negative_int(runner.available_slots)
            if available_slots is not None:
                return available_slots
        if self._runner_configured_max_concurrent_recordings(runner) is not None:
            return max(0, max_concurrent_recordings - active_count)
        return 0

    def build_snapshot(
        self,
        db: Session,
        *,
        worker,
        runner,
        active_jobs_limit: int | None = None,
    ) -> JobRuntimeSnapshot:
        active_job_ids = [job.job_id for job in getattr(worker, "active_jobs", [])]
        active_jobs: list[RecordingJobModel] = []
        if active_job_ids:
            query = (
                db.query(RecordingJobModel)
                .filter(
                    RecordingJobModel.job_id.in_(active_job_ids),
                    RecordingJobModel.status.in_(ACTIVE_RECORDING_STATUSES),
                )
                .order_by(RecordingJobModel.started_at.desc().nullslast(), RecordingJobModel.created_at.desc())
            )
            if active_jobs_limit is not None:
                query = query.limit(active_jobs_limit)
            active_jobs = query.all()

        queued_items = build_queued_item_payloads(db, list(getattr(runner, "queued_items", [])))
        retry_waiting_items = build_retry_waiting_item_payloads(db, list(getattr(runner, "retry_waiting_items", [])))
        queued_job_ids = {
            item["job_id"] for item in queued_items if item.get("kind") == "immediate" and item.get("job_id")
        }
        queued_positions_by_job_id = {
            item["job_id"]: item["queue_position"]
            for item in queued_items
            if item.get("kind") == "immediate" and item.get("job_id")
        }
        retry_waiting_job_ids = {item["job_id"] for item in retry_waiting_items if item.get("job_id")}
        retry_after_by_job_id = {
            item["job_id"]: item["retry_after_sec"] for item in retry_waiting_items if item.get("job_id")
        }
        max_concurrent_recordings = self._runner_max_concurrent_recordings(runner)
        available_slots = self._runner_available_slots(
            runner,
            max_concurrent_recordings=max_concurrent_recordings,
            active_count=len(active_jobs),
        )

        return JobRuntimeSnapshot(
            active_jobs=active_jobs,
            active_job_ids={job.job_id for job in active_jobs},
            queued_items=queued_items,
            retry_waiting_items=retry_waiting_items,
            queue_length=self._runner_queue_length(runner, queued_items),
            retry_waiting_count=self._runner_retry_waiting_count(runner, retry_waiting_items),
            max_concurrent_recordings=max_concurrent_recordings,
            available_slots=available_slots,
            queued_job_ids=queued_job_ids,
            retry_waiting_job_ids=retry_waiting_job_ids,
            queued_positions_by_job_id=queued_positions_by_job_id,
            retry_after_by_job_id=retry_after_by_job_id,
            queued_schedule_items=[item for item in queued_items if item.get("kind") == "schedule"],
        )
