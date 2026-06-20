"""Web UI routes for schedules."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from api import runtime as api_runtime
from api.routes import ui_common
from database.models import Meeting, Schedule, ScheduleType
from database.session import get_db
from services.app_settings import get_all_settings
from services.errors import NotFoundError, ValidationError
from services.runtime_config import RuntimeConfigError
from services.schedule_service import ScheduleCreateData
from utils.cron_helper import cron_to_chinese
from utils.timezone import ensure_utc, utc_now

router = APIRouter(tags=["ui"])
get_app_schedule_service = api_runtime.get_app_schedule_service


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_list(request: Request, db: Session = Depends(get_db)):
    """Schedules list page."""
    schedules = db.query(Schedule).join(Meeting).order_by(Schedule.next_run_at.asc().nullslast(), Meeting.name).all()

    cron_descriptions = {}
    expired_ids = set()
    now = utc_now()

    for schedule in schedules:
        if schedule.cron_expression:
            cron_descriptions[schedule.id] = cron_to_chinese(schedule.cron_expression)

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

    return ui_common.render_template(
        request,
        "schedules/list.html",
        schedules=schedules,
        cron_descriptions=cron_descriptions,
        expired_ids=expired_ids,
        has_expired=len(expired_ids) > 0,
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
            schedule = Schedule(
                meeting_id=source_schedule.meeting_id,
                schedule_type=source_schedule.schedule_type,
                duration_sec=source_schedule.duration_sec,
                lobby_wait_sec=source_schedule.lobby_wait_sec,
                resolution_w=source_schedule.resolution_w,
                resolution_h=source_schedule.resolution_h,
                youtube_enabled=source_schedule.youtube_enabled,
                youtube_privacy=source_schedule.youtube_privacy,
                override_display_name=source_schedule.override_display_name,
                early_join_sec=source_schedule.early_join_sec,
                smart_trim_enabled=source_schedule.smart_trim_enabled,
                dynamic_extension_enabled=source_schedule.dynamic_extension_enabled,
                dynamic_extension_idle_sec=source_schedule.dynamic_extension_idle_sec,
                dynamic_extension_max_sec=source_schedule.dynamic_extension_max_sec,
                cron_expression=source_schedule.cron_expression,
                start_time=None,
            )

    meetings = db.query(Meeting).order_by(Meeting.name).all()
    return ui_common.render_template(
        request,
        "schedules/form.html",
        schedule=schedule,
        meetings=meetings,
        schedule_types=list(ScheduleType),
        app_settings=get_all_settings(db),
    )


@router.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
async def schedules_edit(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    """Edit schedule form."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    meetings = db.query(Meeting).order_by(Meeting.name).all()
    return ui_common.render_template(
        request,
        "schedules/form.html",
        schedule=schedule,
        meetings=meetings,
        schedule_types=list(ScheduleType),
        app_settings=get_all_settings(db),
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
    lobby_wait_sec: int | None = Form(None),
    resolution_preset: str = Form("1080p"),
    resolution_w: int | None = Form(None),
    resolution_h: int | None = Form(None),
    youtube_enabled: bool = Form(False),
    youtube_privacy: str = Form("unlisted"),
    override_display_name: str | None = Form(None),
    early_join_sec: int = Form(30),
    smart_trim_mode: str = Form("inherit"),
    dynamic_extension_mode: str = Form("inherit"),
    dynamic_extension_idle_sec: str | None = Form(None),
    dynamic_extension_max_sec: str | None = Form(None),
):
    """Save schedule (create or update)."""
    duration_sec = duration_min * 60

    if resolution_preset == "1080p":
        resolution_w, resolution_h = 1920, 1080
    elif resolution_preset == "720p":
        resolution_w, resolution_h = 1280, 720

    def mode_to_bool(value: str) -> bool | None:
        if value == "on":
            return True
        if value == "off":
            return False
        return None

    def parse_optional_int(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    schedule_data = {
        "meeting_id": meeting_id,
        "schedule_type": ScheduleType(schedule_type).value,
        "duration_sec": duration_sec,
        "lobby_wait_sec": lobby_wait_sec,
        "resolution_w": resolution_w,
        "resolution_h": resolution_h,
        "youtube_enabled": youtube_enabled,
        "youtube_privacy": youtube_privacy,
        "override_display_name": override_display_name or None,
        "early_join_sec": early_join_sec,
        "smart_trim_enabled": mode_to_bool(smart_trim_mode),
        "dynamic_extension_enabled": mode_to_bool(dynamic_extension_mode),
        "dynamic_extension_idle_sec": parse_optional_int(dynamic_extension_idle_sec),
        "dynamic_extension_max_sec": parse_optional_int(dynamic_extension_max_sec),
    }
    if ScheduleType(schedule_type) == ScheduleType.ONCE and start_time:
        local_dt = datetime.fromisoformat(start_time)
        tz = ZoneInfo(ui_common.settings.timezone)
        local_aware = local_dt.replace(tzinfo=tz)
        utc_dt = local_aware.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        schedule_data["start_time"] = utc_dt
        schedule_data["cron_expression"] = None
    elif ScheduleType(schedule_type) == ScheduleType.CRON and cron_expression:
        schedule_data["cron_expression"] = cron_expression
        schedule_data["start_time"] = None

    try:
        if schedule_id:
            get_app_schedule_service(request).update_schedule(db, schedule_id, schedule_data)
        else:
            get_app_schedule_service(request).create_schedule(db, ScheduleCreateData(**schedule_data))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RedirectResponse(url="/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle", response_class=HTMLResponse)
async def schedules_toggle(
    request: Request,
    schedule_id: int,
    db: Session = Depends(get_db),
    variant: str = Query("row"),
):
    """Toggle schedule enabled state."""
    try:
        schedule = get_app_schedule_service(request).toggle_enabled(db, schedule_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc

    cron_description = cron_to_chinese(schedule.cron_expression) if schedule.cron_expression else None
    template_name = "schedules/_card.html" if variant == "card" else "schedules/_row.html"
    return ui_common.render_template(request, template_name, schedule=schedule, cron_description=cron_description)


@router.post("/schedules/{schedule_id}/trigger", response_class=HTMLResponse)
async def schedules_trigger(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    """Manually trigger a schedule."""
    try:
        result = get_app_schedule_service(request).trigger_schedule(db, schedule_id, manual_trigger=True)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    if not result.accepted:
        raise HTTPException(
            status_code=409,
            detail=result.reason or "Schedule is already running or queued",
        )

    return RedirectResponse(url="/jobs", status_code=303)


@router.delete("/schedules/expired", response_class=HTMLResponse)
async def schedules_delete_expired(request: Request, db: Session = Depends(get_db)):
    """Delete all expired schedules."""
    get_app_schedule_service(request).delete_expired_once_schedules(db, utc_now())
    return HTMLResponse("")


@router.delete("/schedules/{schedule_id}", response_class=HTMLResponse)
async def schedules_delete(request: Request, schedule_id: int, db: Session = Depends(get_db)):
    """Delete schedule."""
    try:
        get_app_schedule_service(request).delete_schedule(db, schedule_id)
    except NotFoundError:
        pass
    return HTMLResponse("")
