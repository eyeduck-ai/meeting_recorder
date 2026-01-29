"""Web UI routes using Jinja2 + HTMX."""

import hmac
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.models import (
    JobStatus,
    Meeting,
    ProviderType,
    RecordingJob,
    Schedule,
    ScheduleType,
    get_db,
)
from recording.remux import pick_preferred_video_path
from recording.worker import get_worker
from scheduling.job_runner import get_job_runner
from scheduling.scheduler import get_scheduler
from services.app_settings import get_all_settings
from utils.cron_helper import cron_to_chinese
from utils.environment import get_environment_status
from utils.timezone import ensure_utc, utc_now

router = APIRouter(tags=["ui"])
settings = get_settings()

# Setup templates
templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# Custom Jinja2 filter: convert UTC to local timezone
def localtime_filter(value: datetime | None, format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Convert UTC datetime to local timezone and format."""
    if not value:
        return "-"
    try:
        tz = ZoneInfo(settings.timezone)
        # If already timezone-aware, strip tzinfo first (assume it's UTC)
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        # Mark as UTC then convert to local
        utc_dt = value.replace(tzinfo=ZoneInfo("UTC"))
        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime(format)
    except Exception:
        return value.strftime(format) if value else "-"


# Register filter
templates.env.filters["localtime"] = localtime_filter


def get_context(request: Request, **kwargs) -> dict:
    """Build template context with common data."""
    env_status = get_environment_status()
    return {
        "request": request,
        "now": utc_now(),
        "auth_enabled": bool(settings.auth_password),
        "env_status": env_status,
        **kwargs,
    }


# =============================================================================
# Login / Logout
# =============================================================================


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = None):
    """Login page."""
    # If no password configured, redirect to home
    if not settings.auth_password:
        return RedirectResponse(url="/", status_code=302)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next_url": next, "error": error},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
):
    """Handle login form submission."""
    if not settings.auth_password:
        return RedirectResponse(url="/", status_code=302)

    if hmac.compare_digest(password, settings.auth_password):
        from api.auth import create_session_token

        response = RedirectResponse(url=next, status_code=302)
        response.set_cookie(
            key="session",
            value=create_session_token(),
            max_age=settings.auth_session_max_age,
            httponly=True,
            samesite="lax",
        )
        return response
    else:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next_url": next, "error": "Invalid password"},
        )


@router.get("/logout")
async def logout():
    """Logout and clear session."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response


# =============================================================================
# Dashboard
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Dashboard home page."""
    # Get recent jobs
    recent_jobs = db.query(RecordingJob).order_by(RecordingJob.created_at.desc()).limit(5).all()

    # Get upcoming schedules
    upcoming_schedules = (
        db.query(Schedule)
        .filter(Schedule.enabled == True, Schedule.next_run_at > utc_now())
        .order_by(Schedule.next_run_at)
        .limit(5)
        .all()
    )

    # Get current job if any (find running job from database)
    running_statuses = [
        JobStatus.STARTING,
        JobStatus.JOINING,
        JobStatus.WAITING_LOBBY,
        JobStatus.RECORDING,
        JobStatus.FINALIZING,
        JobStatus.UPLOADING,
    ]
    current_job = (
        db.query(RecordingJob)
        .filter(RecordingJob.status.in_([s.value for s in running_statuses]))
        .order_by(RecordingJob.created_at.desc())
        .first()
    )
    current_job_id = current_job.job_id if current_job else None

    # Stats
    total_meetings = db.query(Meeting).count()
    total_schedules = db.query(Schedule).count()
    active_schedules = db.query(Schedule).filter(Schedule.enabled == True).count()
    total_jobs = db.query(RecordingJob).count()
    successful_jobs = db.query(RecordingJob).filter(RecordingJob.status == JobStatus.SUCCEEDED).count()

    return templates.TemplateResponse(
        "dashboard.html",
        get_context(
            request,
            recent_jobs=recent_jobs,
            upcoming_schedules=upcoming_schedules,
            current_job_id=current_job_id,
            stats={
                "total_meetings": total_meetings,
                "total_schedules": total_schedules,
                "active_schedules": active_schedules,
                "total_jobs": total_jobs,
                "successful_jobs": successful_jobs,
            },
        ),
    )


# =============================================================================
# Detection Logs
# =============================================================================


@router.get("/detection-logs", response_class=HTMLResponse)
async def detection_logs_page(request: Request):
    """Detection logs viewer page."""
    return templates.TemplateResponse(
        "detection_logs.html",
        get_context(request),
    )


# =============================================================================
# Meetings
# =============================================================================


@router.get("/meetings", response_class=HTMLResponse)
async def meetings_list(request: Request, db: Session = Depends(get_db)):
    """Meetings list page."""
    meetings = db.query(Meeting).order_by(Meeting.created_at.desc()).all()
    return templates.TemplateResponse(
        "meetings/list.html",
        get_context(request, meetings=meetings),
    )


@router.get("/meetings/new", response_class=HTMLResponse)
async def meetings_new(request: Request):
    """New meeting form."""
    return templates.TemplateResponse(
        "meetings/form.html",
        get_context(request, meeting=None, providers=list(ProviderType)),
    )


@router.get("/meetings/{meeting_id}/edit", response_class=HTMLResponse)
async def meetings_edit(request: Request, meeting_id: int, db: Session = Depends(get_db)):
    """Edit meeting form."""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return templates.TemplateResponse(
        "meetings/form.html",
        get_context(request, meeting=meeting, providers=list(ProviderType)),
    )


@router.post("/meetings/save", response_class=HTMLResponse)
async def meetings_save(
    request: Request,
    db: Session = Depends(get_db),
    meeting_id: int | None = Form(None),
    name: str = Form(...),
    provider: str = Form(...),
    meeting_code: str = Form(...),
    site_base_url: str | None = Form(None),
    password: str | None = Form(None),
    default_display_name: str | None = Form(None),
    default_guest_email: str | None = Form(None),
):
    """Save meeting (create or update)."""
    if meeting_id:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
    else:
        meeting = Meeting()
        db.add(meeting)

    meeting.name = name
    meeting.provider = ProviderType(provider)
    meeting.meeting_code = meeting_code
    meeting.site_base_url = site_base_url or None
    meeting.password_encrypted = password or None
    meeting.default_display_name = default_display_name or None
    meeting.default_guest_email = default_guest_email or None

    db.commit()
    return RedirectResponse(url="/meetings", status_code=303)


@router.delete("/meetings/{meeting_id}", response_class=HTMLResponse)
async def meetings_delete(meeting_id: int, db: Session = Depends(get_db)):
    """Delete meeting."""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting:
        db.delete(meeting)
        db.commit()
    return HTMLResponse("")


# =============================================================================
# Schedules
# =============================================================================


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_list(request: Request, db: Session = Depends(get_db)):
    """Schedules list page."""
    # Sort by next_run_at ascending (upcoming first), NULL values last
    schedules = db.query(Schedule).join(Meeting).order_by(Schedule.next_run_at.asc().nullslast(), Meeting.name).all()

    # Compute cron descriptions for each schedule
    cron_descriptions = {}
    # Track expired schedules (ONCE schedules with past start_time and no next_run)
    expired_ids = set()
    now = utc_now()

    for schedule in schedules:
        if schedule.cron_expression:
            cron_descriptions[schedule.id] = cron_to_chinese(schedule.cron_expression)

        # Mark as expired: ONCE schedule with end_time passed OR no next_run_at
        schedule_type = (
            schedule.schedule_type.value if hasattr(schedule.schedule_type, "value") else schedule.schedule_type
        )
        if schedule_type == "once":
            if schedule.start_time:
                start_time = ensure_utc(schedule.start_time)
                if start_time:
                    end_time = start_time + timedelta(seconds=schedule.duration_sec)
                    if now >= end_time:
                        expired_ids.add(schedule.id)
            elif not schedule.next_run_at:
                expired_ids.add(schedule.id)

    return templates.TemplateResponse(
        "schedules/list.html",
        get_context(
            request,
            schedules=schedules,
            cron_descriptions=cron_descriptions,
            expired_ids=expired_ids,
            has_expired=len(expired_ids) > 0,
        ),
    )


@router.get("/schedules/new", response_class=HTMLResponse)
async def schedules_new(
    request: Request,
    copy_from_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """New schedule form, optionally copying from an existing schedule."""
    schedule = None
    if copy_from_id:
        source_schedule = db.query(Schedule).filter(Schedule.id == copy_from_id).first()
        if source_schedule:
            # Create a shallow copy for the form context, but behave as new (no ID)
            # We construct a new Schedule object with copied fields
            schedule = Schedule(
                meeting_id=source_schedule.meeting_id,
                schedule_type=source_schedule.schedule_type,
                duration_sec=source_schedule.duration_sec,
                duration_mode=source_schedule.duration_mode,
                lobby_wait_sec=source_schedule.lobby_wait_sec,
                resolution_w=source_schedule.resolution_w,
                resolution_h=source_schedule.resolution_h,
                dry_run=source_schedule.dry_run,
                youtube_enabled=source_schedule.youtube_enabled,
                youtube_privacy=source_schedule.youtube_privacy,
                override_display_name=source_schedule.override_display_name,
                early_join_sec=source_schedule.early_join_sec,
                min_duration_sec=source_schedule.min_duration_sec,
                stillness_timeout_sec=source_schedule.stillness_timeout_sec,
                # For cron, we copy the expression
                cron_expression=source_schedule.cron_expression,
                # For ONCE, we do NOT copy the start time (default to now/empty)
                # to avoid accidental past scheduling
                start_time=None,
            )

    meetings = db.query(Meeting).order_by(Meeting.name).all()
    return templates.TemplateResponse(
        "schedules/form.html",
        get_context(
            request,
            schedule=schedule,
            meetings=meetings,
            schedule_types=list(ScheduleType),
        ),
    )


@router.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
async def schedules_edit(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    """Edit schedule form."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    meetings = db.query(Meeting).order_by(Meeting.name).all()
    return templates.TemplateResponse(
        "schedules/form.html",
        get_context(
            request,
            schedule=schedule,
            meetings=meetings,
            schedule_types=list(ScheduleType),
        ),
    )


@router.post("/schedules/save", response_class=HTMLResponse)
async def schedules_save(
    request: Request,
    db: Session = Depends(get_db),
    schedule_id: int | None = Form(None),
    meeting_id: int = Form(...),
    schedule_type: str = Form(...),
    start_time: str | None = Form(None),
    duration_min: int = Form(60),
    cron_expression: str | None = Form(None),
    lobby_wait_sec: int = Form(900),
    resolution_preset: str = Form("1080p"),
    resolution_w: int = Form(1920),
    resolution_h: int = Form(1080),
    youtube_enabled: bool = Form(False),
    youtube_privacy: str = Form("unlisted"),
    override_display_name: str | None = Form(None),
    early_join_sec: int = Form(30),
    # Auto-detection settings
    auto_detect_end: bool = Form(False),
    auto_detect_mode: str = Form("after_min"),
    min_duration_min: int | None = Form(None),
    stillness_timeout_sec: int = Form(180),
    dry_run: bool = Form(False),
):
    """Save schedule (create or update)."""
    scheduler = get_scheduler()
    settings = get_settings()

    if schedule_id:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")
        # Remove old schedule from APScheduler
        scheduler.remove_schedule(schedule_id)
    else:
        schedule = Schedule()
        db.add(schedule)

    # Handle duration mode based on auto_detect_end checkbox
    if auto_detect_end:
        duration_mode = "auto"
        schedule.auto_detect_mode = auto_detect_mode
        if auto_detect_mode == "immediate":
            schedule.min_duration_sec = 0
        else:
            schedule.min_duration_sec = min_duration_min * 60 if min_duration_min else 1800  # default 30 min
        duration_sec = settings.max_recording_sec
    else:
        duration_mode = "fixed"
        schedule.auto_detect_mode = None
        schedule.min_duration_sec = None
        duration_sec = duration_min * 60

    # Handle resolution preset
    if resolution_preset == "1080p":
        resolution_w, resolution_h = 1920, 1080
    elif resolution_preset == "720p":
        resolution_w, resolution_h = 1280, 720
    # For "custom", use the provided resolution_w and resolution_h values

    schedule.meeting_id = meeting_id
    schedule.schedule_type = ScheduleType(schedule_type)
    schedule.duration_sec = duration_sec
    schedule.duration_mode = duration_mode
    schedule.lobby_wait_sec = lobby_wait_sec
    schedule.resolution_w = resolution_w
    schedule.resolution_h = resolution_h
    schedule.dry_run = dry_run
    schedule.youtube_enabled = youtube_enabled
    schedule.youtube_privacy = youtube_privacy
    schedule.override_display_name = override_display_name or None
    schedule.early_join_sec = early_join_sec
    schedule.stillness_timeout_sec = stillness_timeout_sec

    if schedule.schedule_type == ScheduleType.ONCE and start_time:
        # Parse local time and convert to UTC for storage
        local_dt = datetime.fromisoformat(start_time)
        tz = ZoneInfo(settings.timezone)
        # Treat input as local time, convert to UTC
        local_aware = local_dt.replace(tzinfo=tz)
        utc_dt = local_aware.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        schedule.start_time = utc_dt
        schedule.cron_expression = None
    elif schedule.schedule_type == ScheduleType.CRON and cron_expression:
        schedule.cron_expression = cron_expression
        schedule.start_time = None

    db.commit()

    # Add to APScheduler if enabled
    if schedule.enabled:
        scheduler.add_schedule(schedule)

    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle", response_class=HTMLResponse)
async def schedules_toggle(
    request: Request,
    schedule_id: int,
    db: Session = Depends(get_db),
    variant: str = Query("row"),
):
    """Toggle schedule enabled state."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    scheduler = get_scheduler()
    schedule.enabled = not schedule.enabled
    db.commit()

    if schedule.enabled:
        scheduler.add_schedule(schedule)
    else:
        scheduler.remove_schedule(schedule_id)

    # Return updated row for HTMX
    cron_description = cron_to_chinese(schedule.cron_expression) if schedule.cron_expression else None
    template_name = "schedules/_card.html" if variant == "card" else "schedules/_row.html"
    return templates.TemplateResponse(
        template_name,
        get_context(request, schedule=schedule, cron_description=cron_description),
    )


@router.post("/schedules/{schedule_id}/trigger", response_class=HTMLResponse)
async def schedules_trigger(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    """Manually trigger a schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    job_runner = get_job_runner()
    job_runner.queue_schedule(schedule.id)

    return RedirectResponse(url="/jobs", status_code=303)


@router.delete("/schedules/expired", response_class=HTMLResponse)
async def schedules_delete_expired(db: Session = Depends(get_db)):
    """Delete all expired schedules."""
    now = utc_now()
    schedules = db.query(Schedule).all()
    scheduler = get_scheduler()

    for schedule in schedules:
        schedule_type = (
            schedule.schedule_type.value if hasattr(schedule.schedule_type, "value") else schedule.schedule_type
        )
        if schedule_type == "once":
            is_expired = False
            if schedule.start_time:
                start_time = ensure_utc(schedule.start_time)
                if start_time:
                    end_time = start_time + timedelta(seconds=schedule.duration_sec)
                    if now >= end_time:
                        is_expired = True
            elif not schedule.next_run_at:
                is_expired = True

            if is_expired:
                scheduler.remove_schedule(schedule.id)
                db.delete(schedule)

    db.commit()
    return HTMLResponse("")


@router.delete("/schedules/{schedule_id}", response_class=HTMLResponse)
async def schedules_delete(schedule_id: int, db: Session = Depends(get_db)):
    """Delete schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule:
        scheduler = get_scheduler()
        scheduler.remove_schedule(schedule_id)
        db.delete(schedule)
        db.commit()
    return HTMLResponse("")


# =============================================================================
# Jobs
# =============================================================================


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Jobs list page."""
    # Define status groups for simplified filtering
    active_statuses = [
        JobStatus.QUEUED,
        JobStatus.STARTING,
        JobStatus.JOINING,
        JobStatus.WAITING_LOBBY,
        JobStatus.RECORDING,
        JobStatus.FINALIZING,
        JobStatus.UPLOADING,
    ]

    query = db.query(RecordingJob).order_by(RecordingJob.created_at.desc())

    # Apply simplified filter
    if status == "active":
        query = query.filter(RecordingJob.status.in_([s.value for s in active_statuses]))
    elif status == "succeeded":
        query = query.filter(RecordingJob.status == JobStatus.SUCCEEDED.value)
    elif status == "failed":
        query = query.filter(RecordingJob.status.in_([JobStatus.FAILED.value, JobStatus.CANCELED.value]))

    jobs = query.limit(50).all()

    # Find currently running job from database
    current_job = (
        db.query(RecordingJob)
        .filter(RecordingJob.status.in_([s.value for s in active_statuses]))
        .order_by(RecordingJob.created_at.desc())
        .first()
    )
    current_job_id = current_job.job_id if current_job else None

    return templates.TemplateResponse(
        "jobs/list.html",
        get_context(
            request,
            jobs=jobs,
            current_job_id=current_job_id,
            selected_status=status,
        ),
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def jobs_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Job detail page."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        "jobs/detail.html",
        get_context(request, job=job),
    )


@router.post("/jobs/{job_id}/stop", response_class=HTMLResponse)
async def jobs_stop(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Stop a running job."""
    # Find the job and check if it's running
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        running_statuses = [
            JobStatus.STARTING.value,
            JobStatus.JOINING.value,
            JobStatus.WAITING_LOBBY.value,
            JobStatus.RECORDING.value,
            JobStatus.FINALIZING.value,
            JobStatus.UPLOADING.value,
        ]
        if job.status in running_statuses:
            worker = get_worker()
            if worker.is_busy and worker._current_job and worker._current_job.job_id == job_id:
                # Worker is running this job - request cancellation
                worker.request_cancel()
            else:
                # Orphaned job - worker is not running it, update DB directly
                job.status = JobStatus.CANCELED.value
                job.error_message = "Stopped by user (job was orphaned)"
                db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/finish", response_class=HTMLResponse)
async def jobs_finish(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Finish a running job early (success path)."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        running_statuses = [
            JobStatus.STARTING.value,
            JobStatus.JOINING.value,
            JobStatus.WAITING_LOBBY.value,
            JobStatus.RECORDING.value,
            JobStatus.FINALIZING.value,
            JobStatus.UPLOADING.value,
        ]
        if job.status in running_statuses:
            worker = get_worker()
            if worker.is_busy and worker._current_job and worker._current_job.job_id == job_id:
                # Worker is running this job - request finish
                worker.request_finish()
            else:
                # Orphaned job - worker is not running it, update DB directly
                job.status = JobStatus.CANCELED.value
                job.error_message = "Finished by user (job was orphaned)"
                db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.delete("/jobs", response_class=HTMLResponse)
async def jobs_delete_all(db: Session = Depends(get_db)):
    """Delete all jobs."""
    db.query(RecordingJob).delete()
    db.commit()
    return HTMLResponse("")


@router.delete("/jobs/{job_id}", response_class=HTMLResponse)
async def jobs_delete(job_id: str, db: Session = Depends(get_db)):
    """Delete job."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        db.delete(job)
        db.commit()
    return HTMLResponse("")


@router.get("/jobs/{job_id}/diagnostics/screenshot", response_class=FileResponse)
async def jobs_screenshot(job_id: str, db: Session = Depends(get_db)):
    """Get job diagnostic screenshot."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job or not job.diagnostic_dir or not job.has_screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    screenshot_path = Path(job.diagnostic_dir) / "screenshot.png"
    if not screenshot_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return FileResponse(screenshot_path, media_type="image/png")


# =============================================================================
# Recordings
# =============================================================================


@router.get("/recordings", response_class=HTMLResponse)
async def recordings_list(request: Request, db: Session = Depends(get_db)):
    """Recordings list page."""
    from uploading.youtube import get_youtube_uploader

    jobs = (
        db.query(RecordingJob)
        .filter(
            RecordingJob.status == JobStatus.SUCCEEDED,
            RecordingJob.output_path != None,
        )
        .order_by(RecordingJob.completed_at.desc())
        .all()
    )

    # Get YouTube status for upload button visibility
    uploader = get_youtube_uploader()
    youtube_configured = settings.youtube_configured
    youtube_authorized = uploader.is_authorized if youtube_configured else False

    return templates.TemplateResponse(
        "recordings/list.html",
        get_context(
            request,
            jobs=jobs,
            youtube_configured=youtube_configured,
            youtube_authorized=youtube_authorized,
        ),
    )


@router.delete("/recordings", response_class=HTMLResponse)
async def recordings_delete_all(db: Session = Depends(get_db)):
    """Delete all recordings (files and database records)."""
    jobs = (
        db.query(RecordingJob)
        .filter(
            RecordingJob.status == JobStatus.SUCCEEDED,
            RecordingJob.output_path != None,
        )
        .all()
    )

    for job in jobs:
        # Try to delete file from disk if it exists
        if job.output_path:
            try:
                file_path = Path(job.output_path)
                candidates = {file_path}
                if file_path.suffix.lower() == ".mkv":
                    candidates.add(file_path.with_suffix(".mp4"))
                elif file_path.suffix.lower() == ".mp4":
                    candidates.add(file_path.with_suffix(".mkv"))

                for candidate in candidates:
                    if candidate.exists():
                        candidate.unlink()
            except Exception as e:
                print(f"Error deleting file {job.output_path}: {e}")

        # Delete job from database
        db.delete(job)

    db.commit()
    return HTMLResponse("")


@router.get("/recordings/{job_id}/download")
async def recordings_download(job_id: str, db: Session = Depends(get_db)):
    """Download recording file."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job or not job.output_path:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = Path(job.output_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Recording file not found")

    file_path = pick_preferred_video_path(file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Recording file not found")

    media_type = "video/mp4" if file_path.suffix.lower() == ".mp4" else "video/x-matroska"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=file_path.name,
    )


@router.delete("/recordings/{job_id}", response_class=HTMLResponse)
async def recordings_delete(job_id: str, db: Session = Depends(get_db)):
    """Delete recording file and job."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if job:
        # Try to delete file from disk if it exists
        if job.output_path:
            try:
                file_path = Path(job.output_path)
                candidates = {file_path}
                if file_path.suffix.lower() == ".mkv":
                    candidates.add(file_path.with_suffix(".mp4"))
                elif file_path.suffix.lower() == ".mp4":
                    candidates.add(file_path.with_suffix(".mkv"))

                for candidate in candidates:
                    if candidate.exists():
                        candidate.unlink()
            except Exception as e:
                print(f"Error deleting file {job.output_path}: {e}")

        # Delete job from database
        db.delete(job)
        db.commit()

    return HTMLResponse("")


# =============================================================================
# Settings
# =============================================================================


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """Settings page."""
    from uploading.youtube import get_youtube_uploader

    uploader = get_youtube_uploader()
    youtube_status = {
        "configured": settings.youtube_configured,
        "authorized": uploader.is_authorized if settings.youtube_configured else False,
    }

    telegram_status = {
        "configured": bool(settings.telegram_bot_token),
    }

    # Get editable settings from database
    app_settings = get_all_settings(db)

    return templates.TemplateResponse(
        "settings.html",
        get_context(
            request,
            youtube_status=youtube_status,
            telegram_status=telegram_status,
            settings=settings,
            app_settings=app_settings,
        ),
    )


# =============================================================================
# Test Center
# =============================================================================
