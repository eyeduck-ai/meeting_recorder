"""UI smoke tests for template rendering and auth flow."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import auth
from api.routes import health, ui, ui_common, ui_jobs, ui_recording_artifacts, ui_recordings, ui_schedules
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
        "api/routes/ui_recording_artifacts.py",
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


def test_settings_template_saves_activity_settings_without_provider_detectors():
    """Detection & Activity settings should no longer expose provider end detector config."""
    template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "settings.html").read_text(encoding="utf-8")

    assert "btn.disabled = true" in template
    assert "saveActivityConfig()" in template
    assert "activitySettingsPayload()" in template
    assert "Detection & Activity Saved" in template
    assert "saveDetectionConfig" not in template
    assert "Detection Saved" not in template
    assert "Provider End Detectors" not in template
    assert "/api/detection/config" not in template
    assert "finally" in template
    assert "btn.disabled = false" in template


def test_detection_logs_template_uses_activity_diagnostics_filters():
    """Detection Logs should present current activity diagnostics, not legacy provider detectors."""
    template = (Path(__file__).resolve().parents[1] / "web" / "templates" / "detection_logs.html").read_text(
        encoding="utf-8"
    )

    assert "media activity and dynamic extension diagnostics" in template
    assert "appendLogFilters" in template
    assert "params.set('detector_type', detector)" in template
    assert "params.set('detected', detected)" in template
    assert "updateExportLink()" in template
    assert "export-csv-link" in template
    assert 'onclick="updateExportLink()"' in template
    assert "data.summary" in template
    assert "renderLogs(data.logs)" in template
    assert "Dynamic Extension" in template
    assert "Media Activity" in template
    assert "meeting end detection events" not in template
    assert "Text Indicator" not in template
    assert "Video Element" not in template
    assert "WebRTC Connection" not in template
    assert "URL Change" not in template
    assert "Screen Freeze" not in template


def test_schedule_templates_do_not_render_legacy_auto_detect_controls():
    """Schedule UI should use smart boundaries instead of legacy provider auto-detect end."""
    repo_root = Path(__file__).resolve().parents[1]
    form_template = (repo_root / "web" / "templates" / "schedules" / "form.html").read_text(encoding="utf-8")
    row_template = (repo_root / "web" / "templates" / "schedules" / "_row.html").read_text(encoding="utf-8")

    assert "auto_detect_end" not in form_template
    assert "auto_detect_mode" not in form_template
    assert "stillness_timeout_sec" not in form_template
    assert "Dry Run (Test Mode)" not in form_template
    assert "duration_mode == 'auto'" not in row_template
    assert "AUTO" not in row_template


def test_trimmed_artifact_removed_flag_tracks_missing_trimmed_file(tmp_path):
    job = SimpleNamespace(trimmed_output_path=str(tmp_path / "recording.trimmed.mkv"))

    ui_recording_artifacts.mark_trimmed_artifact_state(job)

    assert job.trimmed_artifact_removed is True

    existing = tmp_path / "existing.trimmed.mkv"
    existing.write_bytes(b"trimmed")
    job.trimmed_output_path = str(existing)
    ui_recording_artifacts.mark_trimmed_artifact_state(job)

    assert job.trimmed_artifact_removed is False


def test_recording_artifact_state_tracks_preferred_local_download(tmp_path):
    mkv_path = tmp_path / "recording.mkv"
    mp4_path = tmp_path / "recording.mp4"
    mp4_path.write_bytes(b"mp4")
    job = SimpleNamespace(
        output_path=str(mkv_path),
        trimmed_output_path=None,
        raw_output_path=None,
        local_recording_deleted_at=None,
    )

    assert ui_recording_artifacts.preferred_existing_output(job) == mp4_path

    ui_recording_artifacts.mark_recording_artifact_state(job)

    assert job.local_download_available is True
    assert job.trimmed_artifact_removed is False

    job.local_recording_deleted_at = object()
    ui_recording_artifacts.mark_recording_artifact_state(job)

    assert ui_recording_artifacts.preferred_existing_output(job) is None
    assert job.local_download_available is False


def test_trimmed_artifact_state_has_single_ui_owner():
    assert not hasattr(ui_jobs, "_mark_trimmed_artifact_state")
    assert not hasattr(ui_recordings, "_mark_trimmed_artifact_state")


def test_recordings_route_does_not_keep_download_path_wrapper():
    assert not hasattr(ui_recordings, "_resolve_local_download_path")
    assert not hasattr(ui_recordings, "_preferred_existing_output")
    assert not hasattr(ui_recordings, "_delete_recording_files")
    assert not hasattr(ui_recordings, "recording_file_variants")


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
