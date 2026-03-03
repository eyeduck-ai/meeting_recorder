from datetime import timedelta
from types import SimpleNamespace

from telegram_bot.conversations import _parse_duration_minutes, _validate_duration_minutes
from telegram_bot.handlers import _is_schedule_visible
from telegram_bot.notifications import _build_status_message
from utils.timezone import utc_now


def _make_schedule(
    *,
    schedule_type: str = "once",
    next_run_at=None,
    start_time=None,
    duration_sec: int = 3600,
):
    return SimpleNamespace(
        schedule_type=schedule_type,
        next_run_at=next_run_at,
        start_time=start_time,
        duration_sec=duration_sec,
    )


def test_parse_duration_minutes_supports_multiple_formats():
    assert _parse_duration_minutes("45") == 45
    assert _parse_duration_minutes("90m") == 90
    assert _parse_duration_minutes("1h30m") == 90
    assert _parse_duration_minutes("2:15") == 135
    assert _parse_duration_minutes("abc") is None


def test_validate_duration_minutes_respects_bounds(monkeypatch):
    monkeypatch.setattr(
        "telegram_bot.conversations.get_settings",
        lambda: SimpleNamespace(max_recording_sec=7200),
    )
    assert _validate_duration_minutes(0) == "時長必須大於 0 分鐘"
    assert _validate_duration_minutes(121) == "時長不能超過 120 分鐘"
    assert _validate_duration_minutes(120) is None


def test_is_schedule_visible_for_upcoming_and_ongoing_once():
    now = utc_now()

    upcoming = _make_schedule(schedule_type="once", next_run_at=now + timedelta(minutes=30))
    assert _is_schedule_visible(upcoming) is True

    ongoing_once = _make_schedule(
        schedule_type="once",
        start_time=now - timedelta(minutes=10),
        duration_sec=3600,
    )
    assert _is_schedule_visible(ongoing_once) is True

    expired_once = _make_schedule(
        schedule_type="once",
        start_time=now - timedelta(hours=2),
        duration_sec=1800,
    )
    assert _is_schedule_visible(expired_once) is False


def test_failed_notification_includes_mapped_error_reason():
    job = SimpleNamespace(
        meeting_code="room-a",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        file_size=None,
        youtube_enabled=False,
        error_code="JOIN_FAILED",
        error_message=None,
        has_screenshot=False,
        has_html_dump=False,
    )
    message = _build_status_message(job, "failed")
    assert "原因：加入會議失敗" in message


def test_failed_notification_falls_back_to_error_message():
    job = SimpleNamespace(
        meeting_code="room-b",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        file_size=None,
        youtube_enabled=False,
        error_code=None,
        error_message="very long internal failure message for debug",
        has_screenshot=False,
        has_html_dump=False,
    )
    message = _build_status_message(job, "failed")
    assert "原因：" in message
    assert "very long internal failure message for debug" in message
