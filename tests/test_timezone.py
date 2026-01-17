"""Tests for timezone utilities."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from utils.timezone import ensure_utc, from_local, to_local, utc_now


class TestUtcNow:
    """Tests for utc_now function."""

    def test_returns_datetime(self):
        """Should return a datetime object."""
        now = utc_now()
        assert isinstance(now, datetime)

    def test_is_timezone_aware(self):
        """Should return timezone-aware datetime."""
        now = utc_now()
        assert now.tzinfo is not None

    def test_is_utc(self):
        """Should be in UTC timezone."""
        now = utc_now()
        assert now.tzinfo == UTC
        assert now.utcoffset().total_seconds() == 0

    def test_is_recent(self):
        """Should be close to current time."""
        before = datetime.now(UTC)
        now = utc_now()
        after = datetime.now(UTC)
        assert before <= now <= after


class TestEnsureUtc:
    """Tests for ensure_utc function."""

    def test_none_returns_none(self):
        """Should return None for None input."""
        assert ensure_utc(None) is None

    def test_naive_assumes_utc(self):
        """Should assume naive datetime is UTC."""
        naive = datetime(2026, 1, 17, 8, 0, 0)
        result = ensure_utc(naive)

        assert result.tzinfo == UTC
        assert result.hour == 8  # Hour unchanged

    def test_utc_unchanged(self):
        """Should return UTC datetime unchanged."""
        utc = datetime(2026, 1, 17, 8, 0, 0, tzinfo=UTC)
        result = ensure_utc(utc)

        assert result == utc
        assert result.tzinfo == UTC

    def test_converts_other_timezone_to_utc(self):
        """Should convert non-UTC aware datetime to UTC."""
        taipei = ZoneInfo("Asia/Taipei")
        local = datetime(2026, 1, 17, 16, 0, 0, tzinfo=taipei)  # 4 PM Taipei
        result = ensure_utc(local)

        assert result.tzinfo == UTC
        assert result.hour == 8  # 4 PM Taipei = 8 AM UTC


class TestToLocal:
    """Tests for to_local function."""

    def test_none_returns_none(self):
        """Should return None for None input."""
        assert to_local(None) is None

    def test_converts_utc_to_taipei(self):
        """Should convert UTC to Asia/Taipei (UTC+8)."""
        utc = datetime(2026, 1, 17, 0, 0, 0, tzinfo=UTC)
        result = to_local(utc, "Asia/Taipei")

        assert result.hour == 8
        assert str(result.tzinfo) == "Asia/Taipei"

    def test_handles_naive_datetime(self):
        """Should handle naive datetime by assuming UTC."""
        naive = datetime(2026, 1, 17, 0, 0, 0)
        result = to_local(naive, "Asia/Taipei")

        assert result.hour == 8  # 0:00 UTC = 8:00 Taipei

    def test_default_timezone_is_taipei(self):
        """Should default to Asia/Taipei."""
        utc = datetime(2026, 1, 17, 0, 0, 0, tzinfo=UTC)
        result = to_local(utc)

        assert str(result.tzinfo) == "Asia/Taipei"


class TestFromLocal:
    """Tests for from_local function."""

    def test_none_returns_none(self):
        """Should return None for None input."""
        assert from_local(None) is None

    def test_converts_local_to_utc(self):
        """Should convert local time (Asia/Taipei) to UTC."""
        # 16:00 Taipei = 08:00 UTC
        local_naive = datetime(2026, 1, 17, 16, 0, 0)
        result = from_local(local_naive, "Asia/Taipei")

        assert result.tzinfo == UTC
        assert result.hour == 8

    def test_default_timezone_is_taipei(self):
        """Should default to Asia/Taipei."""
        # 16:00 Taipei = 08:00 UTC
        local_naive = datetime(2026, 1, 17, 16, 0, 0)
        result = from_local(local_naive)

        assert result.tzinfo == UTC
        assert result.hour == 8

    def test_handles_already_aware_local(self):
        """Should handle already aware datetime."""
        taipei = ZoneInfo("Asia/Taipei")
        local_aware = datetime(2026, 1, 17, 16, 0, 0, tzinfo=taipei)
        result = from_local(local_aware)

        assert result.tzinfo == UTC
        assert result.hour == 8
