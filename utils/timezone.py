"""Timezone utilities for consistent UTC handling.

This module provides utility functions for consistent timezone handling
across the application. All times in the backend should be timezone-aware UTC.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    """Get current UTC time as timezone-aware datetime.

    Returns:
        Current time in UTC with timezone info attached.

    Example:
        >>> now = utc_now()
        >>> now.tzinfo
        datetime.timezone.utc
    """
    return datetime.now(UTC)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure datetime is timezone-aware UTC.

    - If None, returns None
    - If naive (no timezone), assumes it's UTC and adds tzinfo
    - If aware but not UTC, converts to UTC

    Args:
        dt: Datetime to convert, can be None, naive, or aware

    Returns:
        Timezone-aware UTC datetime, or None if input was None

    Example:
        >>> naive = datetime(2026, 1, 17, 8, 0, 0)
        >>> ensure_utc(naive).tzinfo
        datetime.timezone.utc
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive datetime - assume it's UTC
        return dt.replace(tzinfo=UTC)
    # Already aware - convert to UTC
    return dt.astimezone(UTC)


def to_local(dt: datetime | None, tz_name: str = "Asia/Taipei") -> datetime | None:
    """Convert UTC datetime to local timezone.

    Args:
        dt: UTC datetime to convert
        tz_name: Target timezone name (default: Asia/Taipei)

    Returns:
        Datetime in local timezone, or None if input was None

    Example:
        >>> utc = datetime(2026, 1, 17, 0, 0, 0, tzinfo=timezone.utc)
        >>> local = to_local(utc, "Asia/Taipei")
        >>> local.hour
        8
    """
    if dt is None:
        return None
    dt_utc = ensure_utc(dt)
    return dt_utc.astimezone(ZoneInfo(tz_name))
