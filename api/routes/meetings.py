from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import Meeting, get_db

router = APIRouter(prefix="/meetings", tags=["Meetings"])


class MeetingCreate(BaseModel):
    """Request to create a meeting."""

    name: str = Field(..., min_length=1, max_length=255)
    provider: Literal["jitsi", "webex"] = "jitsi"
    meeting_code: str = Field(..., min_length=1, max_length=255)
    site_base_url: str | None = None
    join_url: str | None = None
    default_display_name: str = "Recorder Bot"
    default_guest_name: str | None = None
    default_guest_email: str | None = None


class MeetingUpdate(BaseModel):
    """Request to update a meeting."""

    name: str | None = None
    meeting_code: str | None = None
    site_base_url: str | None = None
    join_url: str | None = None
    default_display_name: str | None = None
    default_guest_name: str | None = None
    default_guest_email: str | None = None


class MeetingResponse(BaseModel):
    """Meeting response."""

    id: int
    name: str
    provider: str
    meeting_code: str
    site_base_url: str | None
    join_url: str | None
    default_display_name: str
    default_guest_name: str | None
    default_guest_email: str | None
    created_at: str
    updated_at: str


def _to_response(meeting: Meeting) -> MeetingResponse:
    return MeetingResponse(
        id=meeting.id,
        name=meeting.name,
        provider=meeting.provider,
        meeting_code=meeting.meeting_code,
        site_base_url=meeting.site_base_url,
        join_url=meeting.join_url,
        default_display_name=meeting.default_display_name,
        default_guest_name=meeting.default_guest_name,
        default_guest_email=meeting.default_guest_email,
        created_at=meeting.created_at.isoformat() if meeting.created_at else "",
        updated_at=meeting.updated_at.isoformat() if meeting.updated_at else "",
    )


@router.post("/", response_model=MeetingResponse)
async def create_meeting(request: MeetingCreate, db: Session = Depends(get_db)):
    """Create a new meeting configuration."""
    meeting = Meeting(
        name=request.name,
        provider=request.provider,
        meeting_code=request.meeting_code,
        site_base_url=request.site_base_url,
        join_url=request.join_url,
        default_display_name=request.default_display_name,
        default_guest_name=request.default_guest_name,
        default_guest_email=request.default_guest_email,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return _to_response(meeting)


@router.get("/", response_model=list[MeetingResponse])
async def list_meetings(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List all meetings."""
    meetings = db.query(Meeting).order_by(Meeting.created_at.desc()).offset(offset).limit(limit).all()
    return [_to_response(m) for m in meetings]


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(meeting_id: int, db: Session = Depends(get_db)):
    """Get a meeting by ID."""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _to_response(meeting)


@router.put("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    meeting_id: int,
    request: MeetingUpdate,
    db: Session = Depends(get_db),
):
    """Update a meeting."""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(meeting, field, value)

    db.commit()
    db.refresh(meeting)
    return _to_response(meeting)


@router.delete("/{meeting_id}")
async def delete_meeting(meeting_id: int, db: Session = Depends(get_db)):
    """Delete a meeting and all its schedules."""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    db.delete(meeting)
    db.commit()
    return {"message": "Meeting deleted", "id": meeting_id}
