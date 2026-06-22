import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import telegram_bot.notifications as notifications


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
