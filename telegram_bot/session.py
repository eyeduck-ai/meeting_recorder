"""Telegram-owned database session helpers."""

from sqlalchemy.orm import Session

from database.session import get_session_local


def get_db_session() -> Session:
    """Return a database session for Telegram handlers to close."""
    SessionLocal = get_session_local()
    return SessionLocal()
