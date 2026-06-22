import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram.error import BadRequest

import telegram_bot.notifications as notifications
from database.migrations import run_schema_migrations
from database.models import Base, Meeting, Schedule


@pytest.fixture
def notification_session_local(tmp_path, monkeypatch):
    db_path = tmp_path / "notifications.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(notifications, "get_session_local", lambda: SessionLocal)
    return SessionLocal


def test_resolve_meeting_label_uses_schedule_meeting_name(notification_session_local):
    session = notification_session_local()
    try:
        meeting = Meeting(
            name="Glaucoma Review",
            provider="jitsi",
            meeting_code="ophvgh",
            default_display_name="Recorder Bot",
        )
        session.add(meeting)
        session.flush()
        schedule = Schedule(meeting_id=meeting.id)
        session.add(schedule)
        session.commit()
        schedule_id = schedule.id
    finally:
        session.close()

    job = SimpleNamespace(job_id="job-1", schedule_id=schedule_id, meeting_code="ophvgh", display_name="HCH")

    assert notifications._resolve_meeting_label(job) == "Glaucoma Review"


def test_resolve_meeting_label_skips_corrupted_meeting_name(notification_session_local):
    session = notification_session_local()
    try:
        meeting = Meeting(
            name="��|",
            provider="jitsi",
            meeting_code="ophvgh",
            default_display_name="HCH",
        )
        session.add(meeting)
        session.flush()
        schedule = Schedule(meeting_id=meeting.id)
        session.add(schedule)
        session.commit()
        schedule_id = schedule.id
    finally:
        session.close()

    job = SimpleNamespace(job_id="job-1", schedule_id=schedule_id, meeting_code="ophvgh", display_name="HCH")

    assert notifications._resolve_meeting_label(job) == "HCH"


def test_resolve_meeting_label_falls_back_to_meeting_code():
    job = SimpleNamespace(job_id="job-1", schedule_id=None, meeting_code="manual-room", display_name=None)

    assert notifications._resolve_meeting_label(job) == "manual-room"


def test_resolve_meeting_label_skips_generic_display_name():
    job = SimpleNamespace(
        job_id="job-1",
        schedule_id=None,
        meeting_code="manual-room",
        display_name="Recorder Bot",
    )

    assert notifications._resolve_meeting_label(job) == "manual-room"


def test_status_message_sanitizes_explicit_meeting_label():
    job = SimpleNamespace(
        job_id="job-1",
        schedule_id=None,
        provider="jitsi",
        meeting_code="manual-room",
        display_name="Recorder Bot",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        youtube_enabled=False,
        error_code=None,
        error_message=None,
        has_screenshot=False,
        has_html_dump=False,
    )

    message = notifications._build_status_message(job, "recording", meeting_label="��|")

    assert "🎬 Meeting: manual-room" in message
    assert "��|" not in message


def test_status_message_uses_provider_registry_label():
    job = SimpleNamespace(
        job_id="job-1",
        schedule_id=None,
        provider="zoom",
        meeting_code="https://zoom.us/j/123",
        display_name="Recorder Bot",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        youtube_enabled=False,
        error_code=None,
        error_message=None,
        has_screenshot=False,
        has_html_dump=False,
    )

    message = notifications._build_status_message(job, "recording", meeting_label="Zoom Standup")

    assert "Provider: Zoom" in message


def test_status_message_falls_back_for_unknown_provider():
    job = SimpleNamespace(
        job_id="job-1",
        schedule_id=None,
        provider="custom_provider",
        meeting_code="manual-room",
        display_name="Recorder Bot",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        youtube_enabled=False,
        error_code=None,
        error_message=None,
        has_screenshot=False,
        has_html_dump=False,
    )

    message = notifications._build_status_message(job, "recording", meeting_label="Manual")

    assert "Provider: Custom Provider" in message


def test_finalizing_notification_omits_end_and_duration(monkeypatch):
    monkeypatch.setattr(
        "telegram_bot.notifications.get_settings",
        lambda: SimpleNamespace(timezone="Asia/Taipei"),
    )
    job = SimpleNamespace(
        provider="jitsi",
        meeting_code="ophvgh",
        display_name="HCH",
        started_at=datetime(2026, 6, 21, 23, 30),
        completed_at=datetime(2026, 6, 22, 0, 46),
        duration_actual_sec=3320,
        youtube_enabled=True,
        error_code=None,
        error_message=None,
        has_screenshot=False,
        has_html_dump=False,
    )

    message = notifications._build_status_message(job, "finalizing", meeting_label="HCH")

    assert "🎬 Meeting: HCH" in message
    assert "Status: 💾 Finalizing" in message
    assert "Started: 2026-06-22 07:30" in message
    assert "Ended:" not in message
    assert "Duration:" not in message


@pytest.mark.asyncio
async def test_retry_notification_uses_label_fallback_and_english(monkeypatch):
    sent_messages = []

    async def fake_send_or_edit_status_message(*, job, message, notification_type):
        sent_messages.append((job, message, notification_type))
        return 123

    monkeypatch.setattr(notifications, "_send_or_edit_status_message", fake_send_or_edit_status_message)

    job = SimpleNamespace(
        job_id="job-1",
        schedule_id=None,
        provider="jitsi",
        meeting_code="ophvgh",
        display_name="HCH",
        telegram_message_id=None,
    )

    message_id = await notifications.notify_recording_retry(job, 2, 30, "network timeout")

    assert message_id == 123
    assert sent_messages[0][2] == "failure"
    message = sent_messages[0][1]
    assert "🔄 Meeting: HCH" in message
    assert "Provider: Jitsi" in message
    assert "Status: Retrying attempt 2" in message
    assert "Retry in: 30 seconds" in message
    assert "Reason: network timeout" in message
    assert "狀態" not in message


@pytest.mark.asyncio
async def test_status_message_send_timeout_continues_to_next_chat(monkeypatch, caplog):
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, text):
            self.calls.append(("send", chat_id, text))
            if chat_id == 1:
                await asyncio.sleep(1)
            return SimpleNamespace(message_id=chat_id * 100)

    fake_bot = FakeBot()

    async def fake_get_bot():
        return fake_bot

    async def fake_chat_ids(_notification_type):
        return [1, 2]

    monkeypatch.setattr(notifications, "TELEGRAM_NOTIFICATION_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(notifications, "get_bot", fake_get_bot)
    monkeypatch.setattr(notifications, "_get_approved_chat_ids", fake_chat_ids)

    started_at = time.perf_counter()
    with caplog.at_level("WARNING"):
        message_id = await notifications._send_or_edit_status_message(
            job=SimpleNamespace(telegram_message_id=None),
            message="status",
            notification_type="start",
        )
    elapsed = time.perf_counter() - started_at

    assert message_id == 200
    assert [call[:2] for call in fake_bot.calls] == [("send", 1), ("send", 2)]
    assert "Telegram send timed out for chat 1" in caplog.text
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_status_message_fallback_send_timeout_is_best_effort(monkeypatch, caplog):
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def edit_message_text(self, chat_id, message_id, text):
            self.calls.append(("edit", chat_id, message_id, text))
            raise RuntimeError("message is too old to edit")

        async def send_message(self, chat_id, text):
            self.calls.append(("send", chat_id, text))
            await asyncio.sleep(1)

    fake_bot = FakeBot()

    async def fake_get_bot():
        return fake_bot

    async def fake_chat_ids(_notification_type):
        return [1]

    monkeypatch.setattr(notifications, "TELEGRAM_NOTIFICATION_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(notifications, "get_bot", fake_get_bot)
    monkeypatch.setattr(notifications, "_get_approved_chat_ids", fake_chat_ids)

    with caplog.at_level("WARNING"):
        message_id = await notifications._send_or_edit_status_message(
            job=SimpleNamespace(telegram_message_id=99),
            message="status",
            notification_type="start",
        )

    assert message_id == 99
    assert [call[:2] for call in fake_bot.calls] == [("edit", 1), ("send", 1)]
    assert "Telegram fallback-send timed out for chat 1" in caplog.text


@pytest.mark.asyncio
async def test_status_message_noop_edit_does_not_fallback_send(monkeypatch, caplog):
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def edit_message_text(self, chat_id, message_id, text):
            self.calls.append(("edit", chat_id, message_id, text))
            raise BadRequest("Message is not modified: specified new message content is exactly the same")

        async def send_message(self, chat_id, text):
            self.calls.append(("send", chat_id, text))
            return SimpleNamespace(message_id=123)

    fake_bot = FakeBot()

    async def fake_get_bot():
        return fake_bot

    async def fake_chat_ids(_notification_type):
        return [1]

    monkeypatch.setattr(notifications, "get_bot", fake_get_bot)
    monkeypatch.setattr(notifications, "_get_approved_chat_ids", fake_chat_ids)

    with caplog.at_level("ERROR"):
        message_id = await notifications._send_or_edit_status_message(
            job=SimpleNamespace(telegram_message_id=99),
            message="status",
            notification_type="start",
        )

    assert message_id == 99
    assert [call[:2] for call in fake_bot.calls] == [("edit", 1)]
    assert "Telegram edit failed" not in caplog.text


@pytest.mark.asyncio
async def test_telegram_call_helper_catches_callable_exception(caplog):
    def broken_call(**_kwargs):
        raise RuntimeError("cannot create request")

    with caplog.at_level("ERROR"):
        success, result = await notifications._telegram_call_with_timeout(
            broken_call,
            chat_id=123,
            operation="send",
        )

    assert success is False
    assert result is None
    assert "Telegram send failed for chat 123" in caplog.text
