"""Readiness cost / failure isolation and authenticated operations projections."""

import asyncio
import json
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.auth as auth_module
import api.routes.operations as operations_routes
import services.health as health_module
import services.operations as operations_module
from api.routes import health, ui_operations
from database.models import Base, DurableWork, Meeting, RecordingJob, Schedule
from database.notification_models import NotificationDelivery
from services.health import HealthProbe
from services.operations import OperationsService
from utils.timezone import utc_now


@pytest.mark.asyncio
async def test_health_single_flight_cache_does_not_block_event_loop(monkeypatch):
    calls = 0

    def slow_probe():
        nonlocal calls
        calls += 1
        time.sleep(0.08)
        return {"recording_runtime": {"ready": True}, "database_ready": True}

    probe = HealthProbe()
    monkeypatch.setattr(probe, "_probe", slow_probe)
    task = asyncio.gather(*(probe.snapshot() for _ in range(12)))
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.005)
    results = await task
    results[0]["recording_runtime"]["ready"] = False
    assert ticks >= 5
    assert calls == 1
    assert (await probe.snapshot())["recording_runtime"]["ready"] is True
    assert calls == 1


@pytest.mark.asyncio
async def test_health_timeout_keeps_only_one_inflight_worker(monkeypatch):
    completed = threading.Event()
    calls = 0

    def blocked_probe():
        nonlocal calls
        calls += 1
        completed.wait(timeout=2)
        return {"recording_runtime": {"ready": True}, "database_ready": True}

    probe = HealthProbe(timeout_sec=0.02)
    monkeypatch.setattr(probe, "_probe", blocked_probe)
    try:
        assert (await probe.snapshot())["probe_timed_out"] is True
        assert (await probe.snapshot())["probe_timed_out"] is True
        assert calls == 1
        completed.set()
        assert (await probe.snapshot())["database_ready"] is True
    finally:
        completed.set()
        if probe._task is not None:
            await probe._task


def test_health_probe_failure_returns_unready_without_secret_error(monkeypatch):
    def broken():
        raise RuntimeError("secret-credential")

    monkeypatch.setattr(health_module, "get_recording_runtime_status", broken)
    monkeypatch.setattr(health_module, "get_session_local", broken)
    result = HealthProbe._probe()
    assert result["recording_runtime"]["ready"] is False
    assert result["database_ready"] is False
    assert "secret-credential" not in json.dumps(result)


@pytest.fixture
def operations_context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'operations.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(
        operations_module,
        "get_settings",
        lambda: SimpleNamespace(recordings_dir=tmp_path, min_free_disk_gb_before_recording=1),
    )
    with factory() as db:
        meeting = Meeting(
            name="Scheduled Meeting", provider="zoom", meeting_code="https://example.test/?pwd=secret-credential"
        )
        db.add(meeting)
        db.flush()
        db.add(Schedule(meeting_id=meeting.id, enabled=True, next_run_at=utc_now() + timedelta(days=1)))
        db.add_all(
            [
                RecordingJob(
                    job_id="recent",
                    provider="zoom",
                    meeting_code="secret-credential",
                    display_name="Recorder",
                    duration_sec=120,
                    status="succeeded",
                    runtime_summary_json=json.dumps(
                        {"activity_probe": {"samples": 10, "unknown": 2, "debug_token": "secret-credential"}}
                    ),
                ),
                RecordingJob(
                    job_id="legacy",
                    provider="zoom",
                    meeting_code="secret-credential",
                    display_name="Recorder",
                    duration_sec=120,
                    status="failed",
                    runtime_summary_json='["legacy"]',
                ),
                RecordingJob(
                    job_id="invalid-count",
                    provider="zoom",
                    meeting_code="secret-credential",
                    display_name="Recorder",
                    duration_sec=120,
                    status="succeeded",
                    runtime_summary_json=json.dumps(
                        {"activity_probe": {"samples": "invalid", "active": -1, "inactive": None}}
                    ),
                ),
                DurableWork(
                    job_id="recent",
                    kind="upload",
                    payload_json='{"secret":"secret-credential"}',
                    resume_json='{"uri":"secret-credential"}',
                ),
                NotificationDelivery(
                    job_id="recent",
                    event_key="completed:1",
                    chat_id=123,
                    notification_type="complete",
                    phase="completed",
                    state="pending",
                    payload_text="secret-credential",
                ),
            ]
        )
        db.commit()
    app = FastAPI()
    app.state.worker = SimpleNamespace(active_jobs=[])
    app.state.job_runner = SimpleNamespace(
        queued_items=[],
        retry_waiting_items=[],
        queue_length=0,
        retry_waiting_count=0,
        max_concurrent_recordings=2,
        available_slots=2,
    )
    app.state.scheduler = SimpleNamespace(is_running=True)
    probe = HealthProbe()
    monkeypatch.setattr(probe, "_probe", lambda: {"recording_runtime": {"ready": True}, "database_ready": True})
    app.state.health_probe = probe
    yield OperationsService(factory), app
    engine.dispose()


@pytest.mark.asyncio
async def test_operations_live_projection_handles_legacy_data_and_omits_secrets(operations_context):
    service, app = operations_context
    data = await service.snapshot(app)
    assert data["upcoming_schedules"][0]["name"] == "Scheduled Meeting"
    assert data["capacity"]["limit"] == 2
    assert data["probe_totals"] == {"samples": 10, "unknown": 2, "active": 0, "inactive": 0}
    assert data["probe_jobs_with_evidence"] == 1
    assert data["work"]["items"][0]["job_id"] == "recent"
    assert data["notifications"]["items"][0]["chat_id"] == 123
    assert "secret-credential" not in json.dumps(data, default=str)
    assert "resume_json" not in json.dumps(data, default=str)
    assert "payload_text" not in json.dumps(data, default=str)


@pytest.mark.asyncio
async def test_operations_requires_auth_and_readiness_returns_503(operations_context, monkeypatch):
    service, app = operations_context
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(auth_password="test-key", auth_session_secret="test-secret", auth_session_max_age=60),
    )
    monkeypatch.setattr(operations_routes, "OperationsService", lambda: service)
    app.add_middleware(auth_module.AuthMiddleware)
    app.include_router(health.router)
    app.include_router(operations_routes.router, prefix="/api/v1")
    app.include_router(ui_operations.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/operations")).status_code == 401
        assert (await client.get("/operations")).status_code == 302
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        response = await client.get("/api/v1/operations", headers={"X-API-Key": "test-key"})
        assert response.status_code == 200
        assert "secret-credential" not in response.text
        app.state.scheduler.is_running = False
        assert (await client.get("/health/ready")).status_code == 503


@pytest.mark.asyncio
async def test_authenticated_operations_ui_renders_real_schedule_and_delivery_ledger(operations_context, monkeypatch):
    from api.routes import ui_common

    service, app = operations_context
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(auth_password="test-key", auth_session_secret="test-secret", auth_session_max_age=60),
    )
    monkeypatch.setattr(ui_operations, "OperationsService", lambda: service)
    monkeypatch.setattr(ui_common, "get_environment_status", lambda: {"can_record": True})
    with service._session_factory() as db:
        row = db.query(NotificationDelivery).one()
        row.state = "uncertain"
        delivery_id = row.id
        db.commit()
    app.add_middleware(auth_module.AuthMiddleware)
    app.include_router(ui_operations.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("session", auth_module.create_session_token())
        response = await client.get("/operations")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "維運狀態" in response.text
    assert "Scheduled Meeting" in response.text
    assert "Telegram 通知" in response.text
    assert "uncertain" in response.text
    assert f'data-delivery-id="{delivery_id}"' in response.text
    assert "recent" in response.text
    assert "123" in response.text
    assert "secret-credential" not in response.text
    assert "resume_json" not in response.text
    assert "payload_text" not in response.text


@pytest.mark.asyncio
async def test_notification_retry_route_auth_and_explicit_uncertain_confirmation(operations_context, monkeypatch):
    from unittest.mock import AsyncMock

    import telegram_bot.notifications as notifications
    from database.models import TelegramUser
    from services.notification_delivery import DeliveryResult, NotificationDeliveryService

    service, app = operations_context
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(auth_password="test-key", auth_session_secret="test-secret", auth_session_max_age=60),
    )
    with service._session_factory() as db:
        db.add(TelegramUser(chat_id=123, approved=True))
        row = db.query(NotificationDelivery).one()
        row.state = "acknowledged"
        delivery_id = row.id
        db.commit()
    sender = AsyncMock(return_value=DeliveryResult("acknowledged", message_id=777))
    delivery_service = NotificationDeliveryService(service._session_factory, sender)
    monkeypatch.setattr(notifications, "get_notification_delivery_service", lambda: delivery_service)
    app.add_middleware(auth_module.AuthMiddleware)
    app.include_router(operations_routes.router, prefix="/api/v1")
    path = f"/api/v1/operations/notifications/{delivery_id}/retry"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json={})).status_code == 401
        client.headers["X-API-Key"] = "test-key"
        acknowledged = await client.post(path, json={"confirmed_absent": True})
        assert acknowledged.status_code == 409
        assert "already acknowledged" in acknowledged.json()["detail"]
        with service._session_factory() as db:
            row = db.get(NotificationDelivery, delivery_id)
            row.state = "uncertain"
            row.last_error = "timeout"
            db.commit()
        unconfirmed = await client.post(path, json={})
        assert unconfirmed.status_code == 409
        assert "confirm not received" in unconfirmed.json()["detail"]
        assert delivery_service.list_deliveries()[0]["state"] == "uncertain"
        sender.assert_not_awaited()
        confirmed = await client.post(path, json={"confirmed_absent": True})
        assert confirmed.status_code == 200
        assert confirmed.json() == {"queued": True}
        assert delivery_service.list_deliveries()[0]["state"] == "pending"
        # The HTTP operation stores intent; the lifecycle-owned dispatcher sends it.
        sender.assert_not_awaited()
        await delivery_service.retry_due()
        sender.assert_awaited_once()
        assert sender.call_args.args[0] == 123
        assert delivery_service.list_deliveries()[0]["state"] == "acknowledged"
        assert (await client.post(path, json={"confirmed_absent": True})).status_code == 409
        assert (await client.post("/api/v1/operations/notifications/99999/retry", json={})).status_code == 409
        sender.assert_awaited_once()
