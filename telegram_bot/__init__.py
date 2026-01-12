"""Telegram Bot package."""

from sqlalchemy.orm import Session

from database.models import get_session_local


def get_db_session() -> Session:
    """Get a database session.

    Note: The caller is responsible for closing the session.
    """
    SessionLocal = get_session_local()
    return SessionLocal()
