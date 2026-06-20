"""Web UI dashboard routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from api.routes import ui_common
from api.runtime import get_app_job_runner, get_app_job_runtime_state_service, get_app_worker
from database.models import JobStatus, Meeting, RecordingJob, Schedule
from database.session import get_db
from utils.timezone import utc_now

router = APIRouter(tags=["ui"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Dashboard home page."""
    recent_jobs = db.query(RecordingJob).order_by(RecordingJob.created_at.desc()).limit(5).all()

    upcoming_schedules = (
        db.query(Schedule)
        .filter(Schedule.enabled == True, Schedule.next_run_at > utc_now())
        .order_by(Schedule.next_run_at)
        .limit(5)
        .all()
    )

    worker = get_app_worker(request)
    runner = get_app_job_runner(request)
    runtime_snapshot = get_app_job_runtime_state_service(request).build_snapshot(db, worker=worker, runner=runner)

    total_meetings = db.query(Meeting).count()
    total_schedules = db.query(Schedule).count()
    active_schedules = db.query(Schedule).filter(Schedule.enabled == True).count()
    total_jobs = db.query(RecordingJob).count()
    successful_jobs = db.query(RecordingJob).filter(RecordingJob.status == JobStatus.SUCCEEDED).count()

    return ui_common.render_template(
        request,
        "dashboard.html",
        recent_jobs=recent_jobs,
        upcoming_schedules=upcoming_schedules,
        active_jobs=runtime_snapshot.active_jobs,
        queued_items=runtime_snapshot.queued_items,
        retry_waiting_items=runtime_snapshot.retry_waiting_items,
        queue_length=runtime_snapshot.queue_length,
        retry_waiting_count=runtime_snapshot.retry_waiting_count,
        max_concurrent_recordings=runtime_snapshot.max_concurrent_recordings,
        available_slots=runtime_snapshot.available_slots,
        stats={
            "total_meetings": total_meetings,
            "total_schedules": total_schedules,
            "active_schedules": active_schedules,
            "total_jobs": total_jobs,
            "successful_jobs": successful_jobs,
        },
    )


@router.get("/detection-logs", response_class=HTMLResponse)
async def detection_logs_page(request: Request):
    """Detection logs viewer page."""
    return ui_common.render_template(request, "detection_logs.html")
