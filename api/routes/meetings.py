from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database.models import Meeting
from database.session import get_db
from providers import list_providers, validate_provider_name
from services.errors import NotFoundError
from services.meeting_service import MeetingCreateData, get_meeting_service

router = APIRouter(prefix="/meetings", tags=["Meetings"])


class MeetingCreate(BaseModel):
    """Request to create a meeting."""

    name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(default="jitsi", description="Meeting provider", json_schema_extra={"enum": list_providers()})
    meeting_code: str = Field(..., min_length=1, max_length=255)
    site_base_url: str | None = None
    join_url: str | None = None
    password: str | None = None
    default_display_name: str = "Recorder Bot"
    default_guest_name: str | None = None
    default_guest_email: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return validate_provider_name(value)


class MeetingUpdate(BaseModel):
    """Request to update a meeting."""

    name: str | None = None
    provider: str | None = Field(default=None, json_schema_extra={"enum": list_providers()})
    meeting_code: str | None = None
    site_base_url: str | None = None
    join_url: str | None = None
    password: str | None = None
    default_display_name: str | None = None
    default_guest_name: str | None = None
    default_guest_email: str | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str | None) -> str | None:
        return validate_provider_name(value) if value is not None else None


class MeetingResponse(BaseModel):
    """Meeting response."""

    id: int
    name: str
    provider: str
    meeting_code: str
    site_base_url: str | None
    join_url: str | None
    has_password: bool
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
        has_password=meeting.has_password,
        default_display_name=meeting.default_display_name,
        default_guest_name=meeting.default_guest_name,
        default_guest_email=meeting.default_guest_email,
        created_at=meeting.created_at.isoformat() if meeting.created_at else "",
        updated_at=meeting.updated_at.isoformat() if meeting.updated_at else "",
    )


@router.post("/", response_model=MeetingResponse)
async def create_meeting(request: MeetingCreate, db: Session = Depends(get_db)):
    """Create a new meeting configuration."""
    meeting = get_meeting_service().create_meeting(
        db,
        MeetingCreateData(
            name=request.name,
            provider=request.provider,
            meeting_code=request.meeting_code,
            site_base_url=request.site_base_url,
            join_url=request.join_url,
            password=request.password,
            default_display_name=request.default_display_name,
            default_guest_name=request.default_guest_name,
            default_guest_email=request.default_guest_email,
        ),
    )
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
    try:
        meeting = get_meeting_service().update_meeting(
            db,
            meeting_id,
            request.model_dump(exclude_unset=True),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Meeting not found") from exc
    return _to_response(meeting)


@router.delete("/{meeting_id}")
async def delete_meeting(meeting_id: int, db: Session = Depends(get_db)):
    """Delete a meeting and all its schedules."""
    try:
        get_meeting_service().delete_meeting(db, meeting_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Meeting not found") from exc
    return {"message": "Meeting deleted", "id": meeting_id}
