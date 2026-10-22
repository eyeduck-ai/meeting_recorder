"""Persistent Telegram delivery intents; payloads stay internal to the dispatcher."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from utils.timezone import utc_now


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("job_id", "event_key", "chat_id", name="uq_notification_event_chat"),
        Index("ix_notification_due", "state", "next_attempt_at"),
        Index("ix_notification_job_chat", "job_id", "chat_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64))
    event_key: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[int] = mapped_column(Integer)
    notification_type: Mapped[str] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(32))
    event_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    payload_text: Mapped[str] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
