from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.runtime import get_app_job_action_service, get_app_schedule_service
from database.models import Schedule
from database.session import get_db
from services.errors import NotFoundError, ServiceError, ValidationError
from services.runtime_config import RuntimeConfigError
from services.schedule_service import ScheduleCreateData

router = APIRouter(prefix="/schedules", tags=["Schedules"])


class ScheduleCreate(BaseModel):
    """Request to create a schedule."""

    meeting_id: int
    schedule_type: Literal["once", "cron"] = "once"
    start_time: datetime
    duration_sec: int = Field(default=4200, ge=60, le=14400)
    cron_expression: str | None = None
    lobby_wait_sec: int | None = Field(default=None, ge=0, le=1800)
    layout_preset: str = "speaker"
    resolution_w: int | None = Field(default=None, gt=0)
    resolution_h: int | None = Field(default=None, gt=0)
    override_meeting_code: str | None = None
    override_display_name: str | None = None
    youtube_enabled: bool = False
    youtube_privacy: str = "unlisted"
    smart_trim_enabled: bool | None = None
    dynamic_extension_enabled: bool | None = None
    dynamic_extension_idle_sec: int | None = Field(default=None, ge=1, le=14400)
    dynamic_extension_max_sec: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    """Request to update a schedule."""

    schedule_type: Literal["once", "cron"] | None = None
    start_time: datetime | None = None
    duration_sec: int | None = Field(default=None, ge=60, le=14400)
    cron_expression: str | None = None
    lobby_wait_sec: int | None = Field(default=None, ge=0, le=1800)
    layout_preset: str | None = None
    resolution_w: int | None = Field(default=None, gt=0)
    resolution_h: int | None = Field(default=None, gt=0)
    override_meeting_code: str | None = None
    override_display_name: str | None = None
    youtube_enabled: bool | None = None
    youtube_privacy: str | None = None
    smart_trim_enabled: bool | None = None
    dynamic_extension_enabled: bool | None = None
    dynamic_extension_idle_sec: int | None = Field(default=None, ge=1, le=14400)
    dynamic_extension_max_sec: int | None = Field(default=None, ge=0, le=86400)
    enabled: bool | None = None


class ScheduleResponse(BaseModel):
    """Schedule response."""

    id: int
    meeting_id: int
    meeting_name: str | None = None
    schedule_type: str
    start_time: str
    duration_sec: int
    cron_expression: str | None
    lobby_wait_sec: int
    layout_preset: str
    resolution_w: int
    resolution_h: int
    override_meeting_code: str | None
    override_display_name: str | None
    youtube_enabled: bool
    youtube_privacy: str
    smart_trim_enabled: bool | None
    dynamic_extension_enabled: bool | None
    dynamic_extension_idle_sec: int | None
    dynamic_extension_max_sec: int | None
    enabled: bool
    last_run_at: str | None
    last_triggered_at: str | None
    last_started_at: str | None
    last_completed_at: str | None
    next_run_at: str | None
    created_at: str
    updated_at: str


def _to_response(schedule: Schedule) -> ScheduleResponse:
    return ScheduleResponse(
        id=schedule.id,
        meeting_id=schedule.meeting_id,
        meeting_name=schedule.meeting.name if schedule.meeting else None,
        schedule_type=schedule.schedule_type,
        start_time=schedule.start_time.isoformat() if schedule.start_time else "",
        duration_sec=schedule.duration_sec,
        cron_expression=schedule.cron_expression,
        lobby_wait_sec=schedule.lobby_wait_sec,
        layout_preset=schedule.layout_preset,
        resolution_w=schedule.resolution_w,
        resolution_h=schedule.resolution_h,
        override_meeting_code=schedule.override_meeting_code,
        override_display_name=schedule.override_display_name,
        youtube_enabled=schedule.youtube_enabled,
        youtube_privacy=schedule.youtube_privacy,
        smart_trim_enabled=schedule.smart_trim_enabled,
        dynamic_extension_enabled=schedule.dynamic_extension_enabled,
        dynamic_extension_idle_sec=schedule.dynamic_extension_idle_sec,
        dynamic_extension_max_sec=schedule.dynamic_extension_max_sec,
        enabled=schedule.enabled,
        last_run_at=schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        last_triggered_at=schedule.last_triggered_at.isoformat() if schedule.last_triggered_at else None,
        last_started_at=schedule.last_started_at.isoformat() if schedule.last_started_at else None,
        last_completed_at=schedule.last_completed_at.isoformat() if schedule.last_completed_at else None,
        next_run_at=schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        created_at=schedule.created_at.isoformat() if schedule.created_at else "",
        updated_at=schedule.updated_at.isoformat() if schedule.updated_at else "",
    )


@router.post("/", response_model=ScheduleResponse)
async def create_schedule(request: ScheduleCreate, http_request: Request, db: Session = Depends(get_db)):
    """Create a new schedule."""
    try:
        schedule = get_app_schedule_service(http_request).create_schedule(
            db,
            ScheduleCreateData(
                meeting_id=request.meeting_id,
                schedule_type=request.schedule_type,
                start_time=request.start_time,
                duration_sec=request.duration_sec,
                cron_expression=request.cron_expression,
                lobby_wait_sec=request.lobby_wait_sec,
                layout_preset=request.layout_preset,
                resolution_w=request.resolution_w,
                resolution_h=request.resolution_h,
                override_meeting_code=request.override_meeting_code,
                override_display_name=request.override_display_name,
                youtube_enabled=request.youtube_enabled,
                youtube_privacy=request.youtube_privacy,
                smart_trim_enabled=request.smart_trim_enabled,
                dynamic_extension_enabled=request.dynamic_extension_enabled,
                dynamic_extension_idle_sec=request.dynamic_extension_idle_sec,
                dynamic_extension_max_sec=request.dynamic_extension_max_sec,
                enabled=request.enabled,
            ),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _to_response(schedule)


@router.get("/", response_model=list[ScheduleResponse])
async def list_schedules(
    meeting_id: int | None = None,
    enabled: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List schedules with optional filters."""
    query = db.query(Schedule)

    if meeting_id is not None:
        query = query.filter(Schedule.meeting_id == meeting_id)
    if enabled is not None:
        query = query.filter(Schedule.enabled == enabled)

    schedules = query.order_by(Schedule.start_time.desc()).offset(offset).limit(limit).all()
    return [_to_response(s) for s in schedules]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Get a schedule by ID."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _to_response(schedule)


@router.put("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: int,
    request: ScheduleUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Update a schedule."""
    try:
        schedule = get_app_schedule_service(http_request).update_schedule(
            db,
            schedule_id,
            request.model_dump(exclude_unset=True),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _to_response(schedule)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: int, http_request: Request, db: Session = Depends(get_db)):
    """Delete a schedule."""
    try:
        get_app_schedule_service(http_request).delete_schedule(db, schedule_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"message": "Schedule deleted", "id": schedule_id}


@router.post("/{schedule_id}/enable")
async def enable_schedule(schedule_id: int, http_request: Request, db: Session = Depends(get_db)):
    """Enable a schedule."""
    try:
        get_app_schedule_service(http_request).set_enabled(db, schedule_id, True)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"message": "Schedule enabled", "id": schedule_id}


@router.post("/{schedule_id}/disable")
async def disable_schedule(schedule_id: int, http_request: Request, db: Session = Depends(get_db)):
    """Disable a schedule."""
    try:
        get_app_schedule_service(http_request).set_enabled(db, schedule_id, False)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"message": "Schedule disabled", "id": schedule_id}


@router.post("/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: int, http_request: Request, db: Session = Depends(get_db)):
    """Manually trigger a schedule to run now."""
    try:
        result = get_app_schedule_service(http_request).trigger_schedule(db, schedule_id, manual_trigger=True)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.accepted:
        raise HTTPException(
            status_code=409,
            detail=result.reason or "Schedule is already running or queued",
        )

    status_code = 202 if result.status == "queued" else 200
    message = "Schedule queued" if result.status == "queued" else "Schedule triggered"
    return JSONResponse(
        status_code=status_code,
        content={
            "message": message,
            "id": schedule_id,
            "status": result.status,
            "queue_position": result.queue_position,
        },
    )


@router.post("/{schedule_id}/cancel-queued")
async def cancel_queued_schedule(schedule_id: int, http_request: Request, db: Session = Depends(get_db)):
    """Cancel a queued schedule run without disabling the schedule."""
    try:
        get_app_job_action_service(http_request).cancel_queued_schedule(db, schedule_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "message": "Queued schedule run canceled",
        "id": schedule_id,
        "status": "canceled",
    }
