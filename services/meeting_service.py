"""Service layer for meeting write operations."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from database.models import Meeting
from services.errors import NotFoundError


@dataclass(frozen=True)
class MeetingCreateData:
    """Fields required to create a meeting."""

    name: str
    provider: str
    meeting_code: str
    site_base_url: str | None = None
    join_url: str | None = None
    password: str | None = None
    default_display_name: str = "Recorder Bot"
    default_guest_name: str | None = None
    default_guest_email: str | None = None


class MeetingService:
    """Coordinate meeting persistence behavior."""

    def create_meeting(self, db: Session, data: MeetingCreateData) -> Meeting:
        """Create a new meeting and return the persisted model."""
        meeting = Meeting(
            name=data.name,
            provider=data.provider,
            meeting_code=data.meeting_code,
            site_base_url=data.site_base_url,
            join_url=data.join_url,
            meeting_password_plaintext=data.password,
            default_display_name=data.default_display_name,
            default_guest_name=data.default_guest_name,
            default_guest_email=data.default_guest_email,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return meeting

    def update_meeting(self, db: Session, meeting_id: int, updates: dict[str, Any]) -> Meeting:
        """Update a meeting and return the persisted model."""
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise NotFoundError("Meeting not found")

        update_data = dict(updates)
        if "password" in update_data:
            update_data["meeting_password_plaintext"] = update_data.pop("password")

        for field, value in update_data.items():
            setattr(meeting, field, value)

        db.commit()
        db.refresh(meeting)
        return meeting

    def delete_meeting(self, db: Session, meeting_id: int) -> None:
        """Delete a meeting and cascaded schedules."""
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise NotFoundError("Meeting not found")

        db.delete(meeting)
        db.commit()


def get_meeting_service() -> MeetingService:
    """Create a meeting service instance."""
    return MeetingService()
