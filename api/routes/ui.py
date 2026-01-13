"""Web UI routes using Jinja2 + HTMX."""

import hmac
from datetime import datetime
from pathlib import Path

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
from scheduling.job_runner import get_job_runner
from scheduling.scheduler import get_scheduler
from services.app_settings import get_all_settings
from utils.cron_helper import cron_to_chinese

router = APIRouter(tags=["ui"])
settings = get_settings()

# Setup templates
templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def get_context(request: Request, **kwargs) -> dict:
    """Build template context with common data."""
    return {
        "request": request,
        "now": datetime.now(),
        "auth_enabled": bool(settings.auth_password),
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
        .filter(Schedule.enabled == True, Schedule.next_run_at != None)
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
    schedules = db.query(Schedule).join(Meeting).order_by(Meeting.name, Schedule.created_at.desc()).all()

    # Compute cron descriptions for each schedule
    cron_descriptions = {}
    for schedule in schedules:
        if schedule.cron_expression:
            cron_descriptions[schedule.id] = cron_to_chinese(schedule.cron_expression)

    return templates.TemplateResponse(
        "schedules/list.html",
        get_context(request, schedules=schedules, cron_descriptions=cron_descriptions),
    )


@router.get("/schedules/new", response_class=HTMLResponse)
async def schedules_new(request: Request, db: Session = Depends(get_db)):
    """New schedule form."""
    meetings = db.query(Meeting).order_by(Meeting.name).all()
    return templates.TemplateResponse(
        "schedules/form.html",
        get_context(
            request,
            schedule=None,
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
    duration_mode: str = Form("fixed"),
    duration_min: int = Form(60),
    cron_expression: str | None = Form(None),
    lobby_wait_sec: int = Form(900),
    resolution_preset: str = Form("1080p"),
    resolution_w: int = Form(1920),
    resolution_h: int = Form(1080),
    dry_run: bool = Form(False),
    youtube_enabled: bool = Form(False),
    youtube_privacy: str = Form("unlisted"),
    override_display_name: str | None = Form(None),
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

    # Convert duration from minutes to seconds
    # For auto mode, use max_recording_sec as the ceiling
    if duration_mode == "auto":
        duration_sec = settings.max_recording_sec
    else:
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

    if schedule.schedule_type == ScheduleType.ONCE and start_time:
        schedule.start_time = datetime.fromisoformat(start_time)
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
async def schedules_toggle(request: Request, schedule_id: int, db: Session = Depends(get_db)):
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
    return templates.TemplateResponse(
        "schedules/_row.html",
        get_context(request, schedule=schedule, cron_description=cron_description),
    )


@router.post("/schedules/{schedule_id}/trigger", response_class=HTMLResponse)
async def schedules_trigger(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    """Manually trigger a schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    job_runner = get_job_runner()
    await job_runner.queue_schedule(schedule.id)

    return RedirectResponse(url="/jobs", status_code=303)


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
    query = db.query(RecordingJob).order_by(RecordingJob.created_at.desc())
    if status:
        query = query.filter(RecordingJob.status == JobStatus(status))
    jobs = query.limit(50).all()

    # Find currently running job from database
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

    return templates.TemplateResponse(
        "jobs/list.html",
        get_context(
            request,
            jobs=jobs,
            current_job_id=current_job_id,
            statuses=list(JobStatus),
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
            # Mark as canceled in database
            # Note: actual worker cancellation would need additional implementation
            job.status = JobStatus.CANCELED.value
            db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


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


@router.get("/recordings/{job_id}/download")
async def recordings_download(job_id: str, db: Session = Depends(get_db)):
    """Download recording file."""
    job = db.query(RecordingJob).filter(RecordingJob.job_id == job_id).first()
    if not job or not job.output_path:
        raise HTTPException(status_code=404, detail="Recording not found")

    file_path = Path(job.output_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Recording file not found")

    return FileResponse(
        file_path,
        media_type="video/mp4",
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
                if file_path.exists():
                    file_path.unlink()
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
