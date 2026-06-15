"""UI smoke tests for template rendering and auth flow."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import auth
from api.routes import health, ui, ui_common, ui_recordings, ui_schedules
from database.models import AppSettings, Base, Meeting, Schedule
from database.session import get_db
from scheduling.job_runner import QueueScheduleResult
from services.schedule_service import ScheduleService


def test_ui_child_routes_do_not_import_parent_ui_module():
    """UI child routers should not depend on the parent aggregation router."""
    repo_root = Path(__file__).resolve().parents[1]

    for route_file in (
        "api/routes/ui_auth.py",
        "api/routes/ui_dashboard.py",
        "api/routes/ui_meetings.py",
        "api/routes/ui_schedules.py",
        "api/routes/ui_settings.py",
        "api/routes/ui_jobs.py",
        "api/routes/ui_recordings.py",
    ):
        source = (repo_root / route_file).read_text(encoding="utf-8")
        assert "from api.routes import ui as" not in source
        assert "from api.routes.ui import" not in source
        assert "import api.routes.ui" not in source


def test_ui_aggregate_router_includes_jobs_and_recordings_routes():
    """Including the aggregate UI router should expose child UI routes."""
    app = FastAPI()
    app.include_router(ui.router)

    routes = {(route.path, method) for route in app.routes if hasattr(route, "methods") for method in route.methods}

    assert ("/login", "GET") in routes
    assert ("/logout", "GET") in routes
    assert ("/", "GET") in routes
    assert ("/meetings", "GET") in routes
    assert ("/meetings/new", "GET") in routes
    assert ("/schedules", "GET") in routes
    assert ("/schedules/new", "GET") in routes
    assert ("/settings", "GET") in routes
    assert ("/jobs", "GET") in routes
    assert ("/jobs/{job_id}", "GET") in routes
    assert ("/recordings", "GET") in routes
    assert ("/recordings/{job_id}/download", "GET") in routes


def test_settings_template_exposes_recording_crop_top_field():
    """Settings page should expose and submit the recording top-crop fallback."""
    template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "settings.html").read_text(encoding="utf-8")

    assert 'id="recording_browser_mode"' in template
    assert "recording_browser_mode: document.getElementById('recording_browser_mode').value" in template
    assert 'id="recording_crop_mode"' in template
    assert "recording_crop_mode: document.getElementById('recording_crop_mode').value" in template
    assert 'id="recording_frame_warning"' in template
    assert "Normal browser with crop off can record Chrome tabs" in template
    assert "updateRecordingFrameWarning()" in template
    assert 'id="recording_crop_top_px"' in template
    assert "recording_crop_top_px: parseInt" in template


def test_storage_templates_use_maintenance_and_local_removed_state():
    repo_root = Path(__file__).resolve().parents[1]
    settings_template = (repo_root / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
    recordings_template = (repo_root / "web" / "templates" / "recordings" / "list.html").read_text(encoding="utf-8")

    assert "/api/recordings/maintenance" in settings_template
    assert "runMaintenance(true)" in settings_template
    assert "job.local_download_available" in recordings_template
    assert "Local removed" in recordings_template


def test_settings_template_checks_detection_save_failures():
    """Detection settings save should not report success unless both writes succeed."""
    template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "settings.html").read_text(encoding="utf-8")

    assert "btn.disabled = true" in template
    assert "const detectionRes = await fetch('/api/detection/config'" in template
    assert "if (!detectionRes.ok)" in template
    assert "Provider detection settings failed to save" in template
    assert "finally" in template
    assert "btn.disabled = false" in template


def test_trimmed_artifact_removed_flag_tracks_missing_trimmed_file(tmp_path):
    job = SimpleNamespace(trimmed_output_path=str(tmp_path / "recording.trimmed.mkv"))

    ui_recordings._mark_trimmed_artifact_state(job)

    assert job.trimmed_artifact_removed is True

    existing = tmp_path / "existing.trimmed.mkv"
    existing.write_bytes(b"trimmed")
    job.trimmed_output_path = str(existing)
    ui_recordings._mark_trimmed_artifact_state(job)

    assert job.trimmed_artifact_removed is False


@pytest.fixture
def fake_settings():
    """Settings shared by UI routes and auth middleware during tests."""
    return SimpleNamespace(
        auth_password="test-password",
        auth_session_secret="test-secret",
        auth_session_max_age=86400,
        timezone="UTC",
    )


@pytest.fixture
def client(monkeypatch, fake_settings):
    """Test client with auth middleware but without app startup side effects."""
    monkeypatch.setattr(auth, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ui_common, "settings", fake_settings)

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(health.router)
    app.include_router(ui.router)

    with TestClient(app) as test_client:
        yield test_client


def test_health_and_api_smoke(client):
    """Public health routes should remain accessible."""
    health_response = client.get("/health")
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "healthy"
    assert "recording_runtime" in health_response.json()

    api_response = client.get("/api")
    assert api_response.status_code == 200
    assert api_response.json()["status"] == "running"
    assert "recording_runtime" in api_response.json()


def test_login_page_renders(client):
    """Login page should render without template exceptions."""
    response = client.get("/login")

    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert 'action="/login"' in response.text


def test_login_submit_invalid_password_rerenders(client):
    """Invalid credentials should re-render the login page with an error."""
    response = client.post("/login", data={"password": "wrong-password", "next": "/"})

    assert response.status_code == 200
    assert "Invalid password" in response.text
    assert 'name="password"' in response.text


def test_protected_detection_logs_page_requires_auth_and_renders(client, fake_settings):
    """A representative protected HTML page should redirect unauthenticated users and render with auth."""
    redirect_response = client.get("/detection-logs", follow_redirects=False)
    assert redirect_response.status_code == 302
    assert redirect_response.headers["location"] == "/login?next=/detection-logs"

    response = client.get("/detection-logs", headers={"X-API-Key": fake_settings.auth_password})

    assert response.status_code == 200
    assert "Detection Logs" in response.text


def test_meeting_form_exposes_zoom_provider(client, fake_settings):
    """Meeting form should expose Zoom as a supported provider with Zoom-specific helper text."""
    response = client.get("/meetings/new", headers={"X-API-Key": fake_settings.auth_password})

    assert response.status_code == 200
    assert 'option value="zoom"' in response.text
    assert "Meeting URL / ID" in response.text
    assert "Full Zoom invite link is recommended" in response.text


def test_meeting_edit_redacts_password_and_blank_submit_preserves(monkeypatch, fake_settings, tmp_path):
    """Editing a meeting should never echo the stored password and blank password should preserve it."""
    monkeypatch.setattr(auth, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ui_common, "settings", fake_settings)

    engine = create_engine(f"sqlite:///{tmp_path / 'ui-meeting-secret.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    session = SessionLocal()
    try:
        meeting = Meeting(
            name="Secret Meeting",
            provider="jitsi",
            meeting_code="secret-room",
            meeting_password_plaintext="raw-meeting-secret",
            default_display_name="Recorder Bot",
        )
        session.add(meeting)
        session.commit()
        meeting_id = meeting.id
    finally:
        session.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(health.router)
    app.include_router(ui.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        edit_response = test_client.get(
            f"/meetings/{meeting_id}/edit",
            headers={"X-API-Key": fake_settings.auth_password},
        )
        preserve_response = test_client.post(
            "/meetings/save",
            data={
                "meeting_id": str(meeting_id),
                "name": "Secret Meeting Updated",
                "provider": "jitsi",
                "meeting_code": "secret-room",
                "password": "",
                "default_display_name": "Recorder Bot",
            },
            headers={"X-API-Key": fake_settings.auth_password},
            follow_redirects=False,
        )

    assert edit_response.status_code == 200
    assert "raw-meeting-secret" not in edit_response.text
    assert "Password is set" in edit_response.text
    assert preserve_response.status_code == 303

    session = SessionLocal()
    try:
        saved = session.query(Meeting).filter(Meeting.id == meeting_id).first()
        assert saved.meeting_password_plaintext == "raw-meeting-secret"
    finally:
        session.close()


def test_meeting_edit_clear_password(monkeypatch, fake_settings, tmp_path):
    """The clear password checkbox should remove the stored meeting password."""
    monkeypatch.setattr(auth, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ui_common, "settings", fake_settings)

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ui-meeting-clear-secret.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    session = SessionLocal()
    try:
        meeting = Meeting(
            name="Clear Secret Meeting",
            provider="jitsi",
            meeting_code="clear-secret-room",
            meeting_password_plaintext="raw-meeting-secret",
            default_display_name="Recorder Bot",
        )
        session.add(meeting)
        session.commit()
        meeting_id = meeting.id
    finally:
        session.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(health.router)
    app.include_router(ui.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        response = test_client.post(
            "/meetings/save",
            data={
                "meeting_id": str(meeting_id),
                "name": "Clear Secret Meeting",
                "provider": "jitsi",
                "meeting_code": "clear-secret-room",
                "password": "",
                "clear_password": "true",
                "default_display_name": "Recorder Bot",
            },
            headers={"X-API-Key": fake_settings.auth_password},
            follow_redirects=False,
        )

    assert response.status_code == 303
    session = SessionLocal()
    try:
        saved = session.query(Meeting).filter(Meeting.id == meeting_id).first()
        assert saved.meeting_password_plaintext is None
    finally:
        session.close()


def test_new_schedule_form_uses_app_settings_and_copy_preserves_schedule(monkeypatch, fake_settings, tmp_path):
    """New schedules should use app_settings defaults while copy keeps schedule overrides."""
    monkeypatch.setattr(auth, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ui_common, "settings", fake_settings)

    engine = create_engine(f"sqlite:///{tmp_path / 'ui.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    session = SessionLocal()
    try:
        meeting = Meeting(
            name="Runtime Defaults Meeting",
            provider="jitsi",
            meeting_code="runtime-room",
            default_display_name="Recorder Bot",
        )
        session.add(meeting)
        session.flush()
        schedule = Schedule(
            meeting_id=meeting.id,
            schedule_type="once",
            duration_sec=3600,
            lobby_wait_sec=111,
            resolution_w=1280,
            resolution_h=720,
        )
        session.add(schedule)
        session.add_all(
            [
                AppSettings(key="resolution_w", value="1440"),
                AppSettings(key="resolution_h", value="900"),
                AppSettings(key="lobby_wait_sec", value="321"),
            ]
        )
        session.commit()
        schedule_id = schedule.id
    finally:
        session.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(health.router)
    app.include_router(ui.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        new_response = test_client.get("/schedules/new", headers={"X-API-Key": fake_settings.auth_password})
        copy_response = test_client.get(
            f"/schedules/new?copy_from_id={schedule_id}",
            headers={"X-API-Key": fake_settings.auth_password},
        )

    assert new_response.status_code == 200
    assert 'value="1440"' in new_response.text
    assert 'value="900"' in new_response.text
    assert 'value="321"' in new_response.text

    assert copy_response.status_code == 200
    assert 'value="1280"' in copy_response.text
    assert 'value="720"' in copy_response.text
    assert 'value="111"' in copy_response.text


def test_schedule_trigger_redirects_when_queued_and_409_on_duplicate(monkeypatch, fake_settings, tmp_path):
    """Web UI schedule trigger should redirect for accepted work and expose duplicate conflicts."""
    monkeypatch.setattr(auth, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ui_common, "settings", fake_settings)

    engine = create_engine(f"sqlite:///{tmp_path / 'ui-trigger.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    session = SessionLocal()
    try:
        meeting = Meeting(
            name="Trigger Meeting",
            provider="jitsi",
            meeting_code="trigger-room",
            default_display_name="Recorder Bot",
        )
        session.add(meeting)
        session.flush()
        schedule = Schedule(
            meeting_id=meeting.id,
            schedule_type="once",
            duration_sec=3600,
        )
        session.add(schedule)
        session.commit()
        schedule_id = schedule.id
    finally:
        session.close()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    class FakeRunner:
        def __init__(self):
            self.result = QueueScheduleResult(
                accepted=True,
                status="queued",
                schedule_id=schedule_id,
                queue_position=1,
            )

        def queue_schedule(self, schedule_id, manual_trigger=False):
            return self.result

    runner = FakeRunner()
    monkeypatch.setattr(ui_schedules, "get_app_schedule_service", lambda _request: ScheduleService(job_runner=runner))

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(health.router)
    app.include_router(ui.router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        accepted = test_client.post(
            f"/schedules/{schedule_id}/trigger",
            headers={"X-API-Key": fake_settings.auth_password},
            follow_redirects=False,
        )
        runner.result = QueueScheduleResult(
            accepted=False,
            status="duplicate",
            schedule_id=schedule_id,
            reason="Schedule is already running or queued",
        )
        duplicate = test_client.post(
            f"/schedules/{schedule_id}/trigger",
            headers={"X-API-Key": fake_settings.auth_password},
            follow_redirects=False,
        )

    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/jobs"
    assert duplicate.status_code == 409
