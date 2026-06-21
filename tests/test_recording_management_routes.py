import json
from inspect import getsource, signature
from types import SimpleNamespace

import pytest

from api.routes import recording_management


class FakeMaintenanceService:
    def __init__(self):
        self.calls = []

    async def run(self, db, *, dry_run: bool):
        self.calls.append({"db": db, "dry_run": dry_run})
        return {
            "dry_run": dry_run,
            "retention_days": {
                "recordings": 14,
                "diagnostics": 14,
                "logs": 14,
                "detection_logs": 14,
            },
            "canonicalized": [],
            "deleted_recordings": [],
            "deleted_diagnostics": [],
            "deleted_logs": [],
            "deleted_detection_logs": 0,
            "freed_bytes": 123,
            "warnings": [],
            "errors": [],
        }


def _json_response_body(response):
    return json.loads(response.body.decode("utf-8"))


class FakeNotificationChannel:
    def __init__(self):
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return True


@pytest.mark.asyncio
async def test_cleanup_endpoint_is_maintenance_alias(monkeypatch):
    service = FakeMaintenanceService()
    db = object()
    monkeypatch.setattr(recording_management, "get_storage_maintenance_service", lambda: service)

    response = await recording_management.cleanup_recordings(
        max_age_days=3,
        max_count=2,
        dry_run=True,
        db=db,
    )

    body = _json_response_body(response)
    assert service.calls == [{"db": db, "dry_run": True}]
    assert body["dry_run"] is True
    assert body["deprecated_endpoint"]["replacement"] == "/api/recordings/maintenance"
    assert body["deprecated_endpoint"]["ignored_parameters"] == {
        "max_age_days": 3,
        "max_count": 2,
    }


@pytest.mark.asyncio
async def test_check_disk_auto_cleanup_runs_storage_maintenance(monkeypatch):
    class FakeRecordingManager:
        def __init__(self):
            self.calls = 0

        def get_disk_usage(self):
            self.calls += 1
            if self.calls == 1:
                return {"free_gb": 0.5, "recordings_count": 10}
            return {"free_gb": 20.0, "recordings_count": 4}

    manager = FakeRecordingManager()
    service = FakeMaintenanceService()
    db = object()
    monkeypatch.setattr(recording_management, "get_recording_manager", lambda: manager)
    monkeypatch.setattr(recording_management, "get_storage_maintenance_service", lambda: service)

    response = await recording_management.check_disk_space(
        threshold_gb=10.0,
        auto_cleanup=True,
        db=db,
    )

    body = _json_response_body(response)
    assert service.calls == [{"db": db, "dry_run": False}]
    assert body["status"] == "ok"
    assert body["cleanup_performed"] is True
    assert body["cleanup_endpoint"] == "/api/recordings/maintenance"
    assert body["cleanup_result"]["freed_bytes"] == 123
    assert body["usage_after_cleanup"]["free_gb"] == 20.0


@pytest.mark.asyncio
async def test_notification_test_endpoints_do_not_depend_on_db(monkeypatch):
    import services.notification as notification_module

    email = FakeNotificationChannel()
    webhook = FakeNotificationChannel()
    service = SimpleNamespace(
        config=SimpleNamespace(smtp_enabled=True, webhook_enabled=True),
        email=email,
        webhook=webhook,
    )
    monkeypatch.setattr(notification_module, "get_notification_service", lambda: service)

    assert "db" not in signature(recording_management.test_email_notification).parameters
    assert "db" not in signature(recording_management.test_webhook_notification).parameters

    email_response = await recording_management.test_email_notification()
    webhook_response = await recording_management.test_webhook_notification()

    assert _json_response_body(email_response)["status"] == "ok"
    assert _json_response_body(webhook_response)["status"] == "ok"
    assert len(email.calls) == 1
    assert len(webhook.calls) == 1


def test_notification_config_route_uses_service_reset_boundary():
    source = getsource(recording_management.save_notification_config)

    assert "reset_notification_service" in source
    assert "notification_module._notification_service" not in source
