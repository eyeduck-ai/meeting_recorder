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


@pytest.fixture(autouse=True)
def notification_session_local(tmp_path, monkeypatch):
    db_path = tmp_path / "notifications.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(notifications, "get_session_local", lambda: SessionLocal)
    monkeypatch.setattr(notifications, "_delivery_service", None)
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

    async def fake_send_or_edit_status_message(*, job, message, notification_type, **kwargs):
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


def _job(session_factory, *, job_id="job-1", status="recording", chats=(1, 2), **kwargs):
    from database.models import RecordingJob, TelegramUser

    with session_factory() as db:
        for chat_id in chats:
            if db.query(TelegramUser).filter_by(chat_id=chat_id).first() is None:
                db.add(TelegramUser(chat_id=chat_id, approved=True))
        job = RecordingJob(
            job_id=job_id,
            provider="jitsi",
            meeting_code="room",
            display_name="Recorder",
            duration_sec=10,
            status=status,
            **kwargs,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.expunge(job)
        return job


def _bot(monkeypatch, *, send=None, edit=None):
    from unittest.mock import AsyncMock

    bot = SimpleNamespace(
        send_message=send or AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=kw["chat_id"] * 100)),
        edit_message_text=edit or AsyncMock(return_value=True),
    )
    monkeypatch.setattr(notifications, "get_bot", AsyncMock(return_value=bot))
    return bot


@pytest.mark.asyncio
async def test_status_message_send_timeout_continues_to_next_chat(notification_session_local, monkeypatch, caplog):
    from unittest.mock import AsyncMock

    async def send(chat_id, text):
        if chat_id == 1:
            await asyncio.sleep(1)
        return SimpleNamespace(message_id=chat_id * 100)

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=send))
    job = _job(notification_session_local)
    monkeypatch.setattr(notifications, "TELEGRAM_NOTIFICATION_TIMEOUT_SEC", 0.01)
    started_at = time.perf_counter()
    with caplog.at_level("WARNING"):
        message_id = await notifications.notify_recording_status(job, "recording")
    assert message_id == 200
    assert bot.send_message.await_count == 2
    assert "Telegram send timed out for chat 1" in caplog.text
    assert time.perf_counter() - started_at < 0.5
    rows = notifications.get_notification_delivery_service().list_deliveries(job.job_id)
    assert {r["chat_id"]: r["state"] for r in rows} == {1: "uncertain", 2: "acknowledged"}


@pytest.mark.asyncio
async def test_per_chat_message_ids_and_event_dedup(notification_session_local, monkeypatch):
    from database.models import RecordingJob

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, telegram_message_id=777)
    await notifications.notify_recording_status(job, "recording")
    # A legacy scalar message ID cannot establish its chat mapping, even when a
    # deployment happens to have only one recipient today. It is never guessed.
    bot.edit_message_text.assert_not_awaited()
    await notifications.notify_recording_status(job, "recording")
    assert bot.send_message.await_count == 2
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "succeeded"})
        db.commit()
    await notifications.notify_recording_completed(job)
    assert {(c.kwargs["chat_id"], c.kwargs["message_id"]) for c in bot.edit_message_text.await_args_list} == {
        (1, 100),
        (2, 200),
    }
    rows = notifications.get_notification_delivery_service().list_deliveries(job.job_id)
    assert len(rows) == 4
    assert all(r["state"] == "acknowledged" for r in rows)
    assert all(r["acknowledged_at"] is not None for r in rows)
    assert all("payload_text" not in r for r in rows)


@pytest.mark.asyncio
async def test_fallback_replacement_is_remembered_per_chat(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from database.models import RecordingJob

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, chats=(1,))
    await notifications.notify_recording_status(job, "recording")
    bot.edit_message_text.side_effect = BadRequest("message to edit not found")
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=501))
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "finalizing"})
        db.commit()
    assert await notifications.notify_recording_status(job, "finalizing") == 501
    bot.edit_message_text.side_effect = None
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "succeeded"})
        db.commit()
    await notifications.notify_recording_completed(job)
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 501


@pytest.mark.asyncio
async def test_noop_edit_does_not_fallback_send(notification_session_local, monkeypatch):
    from database.models import RecordingJob

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, chats=(1,))
    await notifications.notify_recording_status(job, "recording")
    bot.edit_message_text.side_effect = BadRequest("Message is not modified: same content")
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "succeeded"})
        db.commit()
    assert await notifications.notify_recording_completed(job) == 100
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["complete", "failure", "upload"])
async def test_terminal_notification_uses_own_preference_and_acknowledgment(
    notification_session_local,
    monkeypatch,
    caplog,
    kind,
):
    from database.models import TelegramUser

    job = _job(
        notification_session_local,
        chats=(42,),
        status="failed" if kind == "failure" else "succeeded",
        youtube_enabled=kind == "upload",
        youtube_video_id="video" if kind == "upload" else None,
    )
    with notification_session_local() as db:
        user = db.query(TelegramUser).filter_by(chat_id=42).one()
        user.notify_on_start = False
        user.notify_on_complete = kind == "complete"
        user.notify_on_failure = kind == "failure"
        user.notify_on_upload = kind == "upload"
        db.commit()
    bot = _bot(monkeypatch)
    with caplog.at_level("INFO"):
        if kind == "complete":
            result = await notifications.notify_recording_completed(job)
        elif kind == "failure":
            result = await notifications.notify_recording_failed(job)
        else:
            result = await notifications.notify_youtube_upload_completed(job, "https://youtu.be/video")
    assert result == 4200
    bot.send_message.assert_awaited_once()
    assert f"Telegram API acknowledged job job-1 type={kind}" in caplog.text


@pytest.mark.asyncio
async def test_terminal_overtakes_inflight_stage(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from database.models import RecordingJob

    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=123)

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=send))
    job = _job(notification_session_local, chats=(1,))
    stage = asyncio.create_task(notifications.notify_recording_status(job, "recording"))
    await entered.wait()
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "succeeded"})
        db.commit()
    terminal = asyncio.create_task(notifications.notify_recording_completed(job))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(stage, terminal)
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 123
    assert "Recording completed" in bot.edit_message_text.call_args.kwargs["text"]
    await notifications.notify_recording_status(job, "joining")
    assert bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_failed_notification_does_not_log_success(notification_session_local, monkeypatch, caplog):
    from unittest.mock import AsyncMock

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=RuntimeError("secret_token_in_transport_url")))
    job = _job(notification_session_local, chats=(1,), status="succeeded")
    with caplog.at_level("INFO"):
        assert await notifications.notify_recording_completed(job) is None
    assert bot.send_message.await_count == 1
    assert "acknowledged=0 recipients=1" in caplog.text
    assert "Updated " not in caplog.text
    assert "API acknowledged job" not in caplog.text
    assert "secret_token" not in caplog.text
    row = notifications.get_notification_delivery_service().list_deliveries(job.job_id)[0]
    assert row["state"] == "uncertain"
    assert row["last_error"] == "transport_error"


@pytest.mark.asyncio
async def test_rate_limited_delivery_recovers_after_real_sqlite_restart(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from telegram.error import RetryAfter

    from database.notification_models import NotificationDelivery
    from services.notification_delivery import NotificationDeliveryService
    from utils.timezone import utc_now

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=RetryAfter(1)))
    job = _job(notification_session_local, chats=(1,), status="succeeded")
    assert await notifications.notify_recording_completed(job) is None
    with notification_session_local() as db:
        row = db.query(NotificationDelivery).one()
        assert row.state == "pending"
        row.next_attempt_at = utc_now()
        db.commit()
    # A new dispatcher instance has no process-local state from the first run.
    restarted = NotificationDeliveryService(notification_session_local, notifications._deliver_one_chat)
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=456)
    await restarted.retry_due()
    assert restarted.list_deliveries(job.job_id)[0]["state"] == "acknowledged"
    await restarted.retry_due()
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_restart_does_not_duplicate_unknown_send(notification_session_local, monkeypatch):
    from database.notification_models import NotificationDelivery
    from services.notification_delivery import NotificationDeliveryService

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, chats=(1,), status="succeeded")
    with notification_session_local() as db:
        db.add(
            NotificationDelivery(
                job_id=job.job_id,
                event_key="1:completed",
                chat_id=1,
                phase="completed",
                notification_type="complete",
                payload_text="done",
                state="sending",
                operation="send",
                attempt_count=1,
            )
        )
        db.commit()
    restarted = NotificationDeliveryService(notification_session_local, notifications._deliver_one_chat)
    await restarted.start()
    await asyncio.sleep(0)
    await restarted.shutdown()
    assert restarted.list_deliveries(job.job_id)[0]["state"] == "uncertain"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_retries_safe_edit_and_respects_revoke(notification_session_local, monkeypatch):
    from database.models import TelegramUser
    from database.notification_models import NotificationDelivery
    from services.notification_delivery import NotificationDeliveryService

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, status="succeeded")
    with notification_session_local() as db:
        for chat in (1, 2):
            db.add(
                NotificationDelivery(
                    job_id=job.job_id,
                    event_key="1:completed",
                    chat_id=chat,
                    phase="completed",
                    notification_type="complete",
                    payload_text="done",
                    state="sending",
                    operation="edit",
                    message_id=chat * 100,
                    attempt_count=1,
                )
            )
        db.query(TelegramUser).filter_by(chat_id=2).update({"approved": False})
        db.commit()
    restarted = NotificationDeliveryService(notification_session_local, notifications._deliver_one_chat)
    await restarted.start()
    await restarted.retry_due()
    await restarted.shutdown()
    assert bot.edit_message_text.await_count == 1
    assert bot.edit_message_text.call_args.kwargs["chat_id"] == 1
    assert {r["chat_id"]: r["state"] for r in restarted.list_deliveries(job.job_id)} == {
        1: "acknowledged",
        2: "superseded",
    }


@pytest.mark.asyncio
async def test_retry_is_bounded(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from telegram.error import RetryAfter

    from database.notification_models import NotificationDelivery
    from utils.timezone import utc_now

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=RetryAfter(1)))
    job = _job(notification_session_local, chats=(1,), status="succeeded")
    await notifications.notify_recording_completed(job)
    service = notifications.get_notification_delivery_service()
    for _ in range(8):
        with notification_session_local() as db:
            db.query(NotificationDelivery).update({"next_attempt_at": utc_now()})
            db.commit()
        await service.retry_due()
    row = service.list_deliveries(job.job_id)[0]
    assert row["state"] == "failed"
    assert row["attempt_count"] == 5
    assert bot.send_message.await_count == 5


@pytest.mark.asyncio
async def test_telegram_call_helper_catches_callable_exception(caplog):
    def broken_call(**_kwargs):
        raise RuntimeError("cannot create request")

    with caplog.at_level("ERROR"):
        success, result = await notifications._telegram_call_with_timeout(broken_call, chat_id=123, operation="send")
    assert success is False
    assert result is None


@pytest.mark.asyncio
async def test_retry_notification_follows_failed_recording_attempt(notification_session_local, monkeypatch):
    from database.models import RecordingJob

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, chats=(1,))
    await notifications.notify_recording_status(job, "recording")
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "failed"})
        db.commit()
    assert await notifications.notify_recording_retry(job, 1, 30, "connection failed") == 100
    assert "Retrying attempt 1" in bot.edit_message_text.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_terminal_without_recipients_supersedes_pending_stage(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from telegram.error import RetryAfter

    from database.models import RecordingJob, TelegramUser

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=RetryAfter(30)))
    job = _job(notification_session_local, chats=(1,))
    await notifications.notify_recording_status(job, "recording")
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "succeeded"})
        db.query(TelegramUser).filter_by(chat_id=1).update({"notify_on_complete": False})
        db.commit()
    await notifications.notify_recording_completed(job)
    rows = notifications.get_notification_delivery_service().list_deliveries(job.job_id)
    assert len(rows) == 1
    assert rows[0]["state"] == "superseded"
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_stale_failed_event_does_not_supersede_new_attempt(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from telegram.error import RetryAfter

    from database.models import RecordingJob

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=RetryAfter(1)))
    job = _job(notification_session_local, chats=(1,), attempt_no=2)
    await notifications.notify_recording_status(job, "recording")
    stale = RecordingJob(
        job_id=job.job_id,
        attempt_no=1,
        status="failed",
        provider="jitsi",
        meeting_code="room",
        display_name="Recorder",
        duration_sec=10,
    )
    await notifications.notify_recording_failed(stale)
    rows = notifications.get_notification_delivery_service().list_deliveries(job.job_id)
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["event_key"] == "2:recording"
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_fallback_timeout_is_uncertain_not_safe_edit_retry(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from database.models import RecordingJob
    from database.notification_models import NotificationDelivery

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, chats=(1,))
    await notifications.notify_recording_status(job, "recording")
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"status": "succeeded"})
        db.commit()
    bot.edit_message_text.side_effect = BadRequest("message to edit not found")

    async def hang(**kwargs):
        await asyncio.sleep(1)

    bot.send_message = AsyncMock(side_effect=hang)
    monkeypatch.setattr(notifications, "TELEGRAM_NOTIFICATION_TIMEOUT_SEC", 0.01)
    await notifications.notify_recording_completed(job)
    with notification_session_local() as db:
        row = db.query(NotificationDelivery).filter_by(phase="completed").one()
        assert row.state == "uncertain"
        assert row.operation == "send"
        assert row.message_id is None
    await notifications.get_notification_delivery_service().retry_due()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_fanout_is_bounded(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    active = peak = 0

    async def send(chat_id, text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return SimpleNamespace(message_id=chat_id * 100)

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=send))
    job1 = _job(notification_session_local, job_id="a", chats=tuple(range(1, 8)))
    job2 = _job(notification_session_local, job_id="b", chats=())
    await asyncio.gather(
        notifications.notify_recording_status(job1, "recording"),
        notifications.notify_recording_status(job2, "recording"),
    )
    assert peak <= notifications.TELEGRAM_NOTIFICATION_FANOUT_CONCURRENCY
    assert bot.send_message.await_count == 14


@pytest.mark.asyncio
async def test_new_video_reference_is_new_event_and_old_link_cannot_overwrite_it(
    notification_session_local, monkeypatch
):
    from database.models import RecordingJob

    bot = _bot(monkeypatch)
    job = _job(
        notification_session_local, chats=(1,), status="succeeded", youtube_enabled=True, youtube_video_id="first"
    )
    await notifications.notify_youtube_upload_completed(job, "https://youtu.be/first")
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"youtube_video_id": "second"})
        db.commit()
    job.youtube_video_id = "second"
    await notifications.notify_youtube_upload_completed(job, "https://youtu.be/second")
    assert bot.edit_message_text.await_count == 1
    assert "https://youtu.be/second" in bot.edit_message_text.call_args.kwargs["text"]
    job.youtube_video_id = "first"
    await notifications.notify_youtube_upload_completed(job, "https://youtu.be/first")
    assert bot.edit_message_text.await_count == 1
    rows = notifications.get_notification_delivery_service().list_deliveries(job.job_id)
    assert {r["event_key"] for r in rows} == {"1:uploaded:first", "1:uploaded:second"}


@pytest.mark.asyncio
async def test_manual_retry_does_not_repeat_ack_or_unconfirmed_uncertain(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=RuntimeError("offline")))
    job = _job(notification_session_local, chats=(1,), status="succeeded")
    await notifications.notify_recording_completed(job)
    service = notifications.get_notification_delivery_service()
    delivery_id = service.list_deliveries(job.job_id)[0]["id"]
    with pytest.raises(ValueError, match="confirm not received"):
        service.retry_failed(delivery_id)
    await service.retry_due()
    assert bot.send_message.await_count == 1
    assert service.retry_failed(delivery_id, confirmed_absent=True)
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=888)
    await service.retry_due()
    assert bot.send_message.await_count == 2
    assert service.list_deliveries(job.job_id)[0]["state"] == "acknowledged"
    assert not service.retry_failed(delivery_id, confirmed_absent=True)
    await service.retry_due()
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_manual_retry_rejects_stale_terminal_event(notification_session_local, monkeypatch):
    from unittest.mock import AsyncMock

    from database.models import RecordingJob

    bot = _bot(monkeypatch, send=AsyncMock(side_effect=BadRequest("invalid message")))
    job = _job(notification_session_local, chats=(1,), status="succeeded", youtube_enabled=True)
    await notifications.notify_recording_completed(job)
    service = notifications.get_notification_delivery_service()
    delivery_id = service.list_deliveries(job.job_id)[0]["id"]
    with notification_session_local() as db:
        db.query(RecordingJob).filter_by(job_id=job.job_id).update({"youtube_video_id": "finished"})
        db.commit()
    assert not service.retry_failed(delivery_id)
    assert service.list_deliveries(job.job_id)[0]["state"] == "superseded"
    await service.retry_due()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ack_commit_failure_does_not_stay_sending_or_duplicate(notification_session_local, monkeypatch):
    from database.notification_models import NotificationDelivery

    bot = _bot(monkeypatch)
    job = _job(notification_session_local, chats=(1,), status="succeeded")
    original_commit = notification_session_local.class_.commit
    failed = False

    def fail_ack_once(session):
        nonlocal failed
        if not failed and any(
            isinstance(row, NotificationDelivery) and row.state == "acknowledged" for row in session.dirty
        ):
            failed = True
            raise RuntimeError("temporary database write failure")
        return original_commit(session)

    monkeypatch.setattr(notification_session_local.class_, "commit", fail_ack_once)
    assert await notifications.notify_recording_completed(job) is None
    service = notifications.get_notification_delivery_service()
    assert service.list_deliveries(job.job_id)[0]["state"] == "sending"
    await service.retry_due()
    assert service.list_deliveries(job.job_id)[0]["state"] == "uncertain"
    bot.send_message.assert_awaited_once()
