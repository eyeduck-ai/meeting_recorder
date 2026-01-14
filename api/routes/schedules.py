from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import Meeting, Schedule, get_db
from scheduling.scheduler import get_scheduler

router = APIRouter(prefix="/schedules", tags=["Schedules"])


class ScheduleCreate(BaseModel):
    """Request to create a schedule."""

    meeting_id: int
    schedule_type: Literal["once", "cron"] = "once"
    start_time: datetime
    duration_sec: int = Field(default=4200, ge=60, le=14400)
    cron_expression: str | None = None
    lobby_wait_sec: int = Field(default=900, ge=0, le=1800)
    layout_preset: str = "speaker"
    resolution_w: int = 1920
    resolution_h: int = 1080
    override_meeting_code: str | None = None
    override_display_name: str | None = None
    youtube_enabled: bool = False
    youtube_privacy: str = "unlisted"
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    """Request to update a schedule."""

    schedule_type: Literal["once", "cron"] | None = None
    start_time: datetime | None = None
    duration_sec: int | None = None
    cron_expression: str | None = None
    lobby_wait_sec: int | None = None
    layout_preset: str | None = None
    override_meeting_code: str | None = None
    override_display_name: str | None = None
    youtube_enabled: bool | None = None
    youtube_privacy: str | None = None
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
    enabled: bool
    last_run_at: str | None
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
        enabled=schedule.enabled,
        last_run_at=schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        next_run_at=schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        created_at=schedule.created_at.isoformat() if schedule.created_at else "",
        updated_at=schedule.updated_at.isoformat() if schedule.updated_at else "",
    )


@router.post("/", response_model=ScheduleResponse)
async def create_schedule(request: ScheduleCreate, db: Session = Depends(get_db)):
    """Create a new schedule."""
    # Verify meeting exists
    meeting = db.query(Meeting).filter(Meeting.id == request.meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Validate cron expression for cron schedules
    if request.schedule_type == "cron" and not request.cron_expression:
        raise HTTPException(
            status_code=400,
            detail="cron_expression is required for cron schedule type",
        )

    schedule = Schedule(
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
        enabled=request.enabled,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    # Add to scheduler if enabled
    if schedule.enabled:
        scheduler = get_scheduler()
        if scheduler.is_running:
            scheduler.add_schedule(schedule)

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
    db: Session = Depends(get_db),
):
    """Update a schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(schedule, field, value)

    db.commit()
    db.refresh(schedule)

    # Update scheduler
    scheduler = get_scheduler()
    if scheduler.is_running:
        scheduler.update_schedule(schedule)

    return _to_response(schedule)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Delete a schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Remove from scheduler
    scheduler = get_scheduler()
    if scheduler.is_running:
        scheduler.remove_schedule(schedule_id)

    db.delete(schedule)
    db.commit()
    return {"message": "Schedule deleted", "id": schedule_id}


@router.post("/{schedule_id}/enable")
async def enable_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Enable a schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    schedule.enabled = True
    db.commit()
    db.refresh(schedule)

    scheduler = get_scheduler()
    if scheduler.is_running:
        scheduler.add_schedule(schedule)

    return {"message": "Schedule enabled", "id": schedule_id}


@router.post("/{schedule_id}/disable")
async def disable_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Disable a schedule."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    schedule.enabled = False
    db.commit()

    scheduler = get_scheduler()
    if scheduler.is_running:
        scheduler.remove_schedule(schedule_id)

    return {"message": "Schedule disabled", "id": schedule_id}


@router.post("/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Manually trigger a schedule to run now."""
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    from scheduling.job_runner import get_job_runner

    runner = get_job_runner()

    if runner.is_busy:
        raise HTTPException(
            status_code=409,
            detail="Worker is busy. Job will be queued.",
        )

    runner.queue_schedule(schedule_id)
    return {"message": "Schedule triggered", "id": schedule_id}
