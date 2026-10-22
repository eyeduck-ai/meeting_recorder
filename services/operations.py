"""Read-only operations view. Never expose meeting secrets or upload session URLs."""

import asyncio
import os
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func

from config.settings import get_settings
from database.models import RecordingJob, Schedule
from database.notification_models import NotificationDelivery
from database.session import get_session_local
from services.durable_work import DurableWorkStore
from services.health import get_health_probe
from services.job_runtime_state import JobRuntimeStateService
from utils.timezone import ensure_utc, utc_now


def _probe_view(job) -> dict | None:
    summary = job.runtime_summary
    probe = summary.get("activity_probe") if isinstance(summary, dict) else None
    if not isinstance(probe, dict):
        return None
    counts = {}
    for name in ("samples", "unknown", "active", "inactive"):
        value = probe.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[name] = value
    return counts or None


def _job_view(job) -> dict:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "recording_started_at": job.recording_started_at,
        "completed_at": job.completed_at,
        "duration_actual_sec": job.duration_actual_sec,
        "trim_status": job.trim_status,
        "trim_start_sec": job.trim_start_sec,
        "trim_end_sec": job.trim_end_sec,
        "dynamic_extension_stop_reason": job.dynamic_extension_stop_reason,
        "error_code": job.error_code,
        "failure_stage": job.failure_stage,
        "youtube_video_id": job.youtube_video_id,
        "youtube_uploaded_at": job.youtube_uploaded_at,
        "probe": _probe_view(job),
    }


class OperationsService:
    def __init__(self, session_factory=None):
        self._session_factory = session_factory or get_session_local()

    def database_snapshot(self, *, worker, runner) -> dict:
        with self._session_factory() as db:
            runtime = JobRuntimeStateService().build_snapshot(db, worker=worker, runner=runner)
            recent = db.query(RecordingJob).order_by(RecordingJob.created_at.desc()).limit(30).all()
            upcoming = (
                db.query(Schedule)
                .filter(Schedule.enabled == True, Schedule.next_run_at >= utc_now())
                .order_by(Schedule.next_run_at)
                .limit(10)
                .all()
            )
            latest = db.query(Schedule).order_by(Schedule.last_triggered_at.desc()).first()
            notification_counts = dict(
                db.query(NotificationDelivery.state, func.count()).group_by(NotificationDelivery.state).all()
            )
            deliveries = db.query(NotificationDelivery).order_by(NotificationDelivery.id.desc()).limit(50).all()
            pending = (
                db.query(NotificationDelivery.created_at)
                .filter(NotificationDelivery.state.in_(["pending", "sending"]))
                .order_by(NotificationDelivery.created_at)
                .first()
            )
            probes = [_probe_view(job) for job in recent]
            probe_counts = Counter()
            for probe in probes:
                if isinstance(probe, dict):
                    for name in ("samples", "unknown", "active", "inactive"):
                        probe_counts[name] += probe.get(name, 0)
            settings = get_settings()
            try:
                disk = shutil.disk_usage(Path(settings.recordings_dir))
                free_bytes, total_bytes = disk.free, disk.total
            except OSError:
                free_bytes, total_bytes = 0, 0
            return {
                "revision": os.environ.get("APP_REVISION", "source checkout"),
                "generated_at": utc_now(),
                "capacity": {
                    "active": runtime.active_count,
                    "available": runtime.available_slots,
                    "limit": runtime.max_concurrent_recordings,
                    "queued": runtime.queue_length,
                    "retry_waiting": runtime.retry_waiting_count,
                },
                "disk": {
                    "free_bytes": free_bytes,
                    "total_bytes": total_bytes,
                    "minimum_free_bytes": int(settings.min_free_disk_gb_before_recording * 1024**3),
                    "ready": total_bytes > 0 and free_bytes >= settings.min_free_disk_gb_before_recording * 1024**3,
                },
                "recent_jobs": [_job_view(job) for job in recent],
                "upcoming_schedules": [
                    {"id": s.id, "name": s.meeting.name, "next_run_at": s.next_run_at} for s in upcoming
                ],
                "last_schedule_triggered_at": latest.last_triggered_at if latest else None,
                "probe_totals": dict(probe_counts),
                "probe_jobs_with_evidence": sum(isinstance(p, dict) for p in probes),
                "notifications": {
                    "counts": notification_counts,
                    "oldest_pending_age_sec": max(0, (utc_now() - ensure_utc(pending[0])).total_seconds())
                    if pending
                    else None,
                    "items": [
                        {
                            k: getattr(row, k)
                            for k in (
                                "id",
                                "job_id",
                                "chat_id",
                                "phase",
                                "state",
                                "message_id",
                                "attempt_count",
                                "last_error",
                                "acknowledged_at",
                                "next_attempt_at",
                            )
                        }
                        for row in deliveries
                    ],
                },
            }

    async def snapshot(self, app) -> dict:
        # Capture process-local mutable registries on their owning event loop;
        # only detached inputs and a new DB session cross into the I/O thread.
        worker = SimpleNamespace(active_jobs=list(app.state.worker.active_jobs))
        runner_fields = {}
        for name in (
            "queued_items",
            "retry_waiting_items",
            "queue_length",
            "retry_waiting_count",
            "max_concurrent_recordings",
            "available_slots",
        ):
            if hasattr(app.state.job_runner, name):
                value = getattr(app.state.job_runner, name)
                runner_fields[name] = list(value) if name.endswith("items") else value
        data = await asyncio.to_thread(self.database_snapshot, worker=worker, runner=SimpleNamespace(**runner_fields))
        data["work"] = await asyncio.to_thread(DurableWorkStore(lambda: self._session_factory).snapshot)
        data["health"] = await get_health_probe(app).snapshot()
        data["scheduler_running"] = bool(app.state.scheduler.is_running)
        return data
