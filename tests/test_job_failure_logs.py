"""Tests for job failure log rendering and access."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import auth
from api.routes import ui
from database.models import JobStatus


def _make_job(status: str, diagnostic_dir: str | None = None):
    """Create a lightweight job object for template rendering."""
    now = datetime(2024, 1, 15, 12, 0, 0)
    return SimpleNamespace(
        job_id="job12345",
        status=SimpleNamespace(value=status),
        provider="jitsi",
        meeting_code="test-room",
        display_name="Recorder Bot",
        schedule=None,
        created_at=now,
        started_at=now,
        joined_at=None,
        recording_started_at=None,
        recording_stopped_at=None,
        completed_at=now,
        youtube_enabled=False,
        youtube_uploaded_at=None,
        youtube_video_id=None,
        output_path=None,
        file_size=None,
        duration_actual_sec=None,
        duration_sec=3600,
        error_code=SimpleNamespace(value="JOIN_TIMEOUT"),
        error_message="Join timed out after 60 seconds",
        diagnostic_dir=diagnostic_dir,
        has_screenshot=False,
        has_html_dump=False,
        has_console_log=bool(diagnostic_dir),
    )


@pytest.fixture
def ui_client_factory(monkeypatch, mock_settings):
    """Create a UI test client with an injected mock DB session."""

    monkeypatch.setattr(auth, "get_settings", lambda: mock_settings)
    monkeypatch.setattr(ui, "settings", mock_settings)
    monkeypatch.setattr(
        ui,
        "get_environment_status",
        lambda: SimpleNamespace(is_recording_capable=True, warning_message=None),
    )

    def build_client(db_session: Mock) -> TestClient:
        app = FastAPI()
        app.add_middleware(auth.AuthMiddleware)
        app.include_router(ui.router)
        app.dependency_overrides[ui.get_db] = lambda: db_session
        return TestClient(app)

    return build_client


def test_read_text_excerpt_returns_tail_when_file_is_large(tmp_path):
    """Large files should be truncated to the last 64 KiB."""
    log_path = tmp_path / "console.log"
    content = ("A" * 1024) + ("B" * (ui.JOB_LOG_EXCERPT_BYTES + 128))
    log_path.write_text(content, encoding="utf-8")

    excerpt, truncated = ui._read_text_excerpt(log_path)

    assert truncated is True
    assert excerpt == content[-ui.JOB_LOG_EXCERPT_BYTES :]


def test_resolve_job_log_path_rejects_unknown_or_escaped_names(tmp_path):
    """Helper should only resolve known filenames within the diagnostics directory."""
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    (diagnostics_dir / "console.log").write_text("console output", encoding="utf-8")
    job = _make_job(JobStatus.FAILED.value, str(diagnostics_dir))

    assert ui._resolve_job_log_path(job, "console.log") == (diagnostics_dir / "console.log").resolve()
    assert ui._resolve_job_log_path(job, "app.log") is None
    assert ui._resolve_job_log_path(job, "../console.log") is None


def test_jobs_detail_renders_failure_summary_and_logs(ui_client_factory, mock_settings, tmp_path):
    """Failed jobs should show extracted failure context and raw per-job logs."""
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    (diagnostics_dir / "metadata.json").write_text(
        json.dumps(
            {
                "error_code": "JOIN_TIMEOUT",
                "error_message": "Timeout waiting for host approval",
                "stage": "waiting_lobby",
                "url": "https://meet.jit.si/test-room",
                "title": "Waiting for the host",
            }
        ),
        encoding="utf-8",
    )
    (diagnostics_dir / "console.log").write_text("[log] prejoin loaded", encoding="utf-8")
    (diagnostics_dir / "ffmpeg.log").write_text("ffmpeg version test-build", encoding="utf-8")

    job = _make_job(JobStatus.FAILED.value, str(diagnostics_dir))
    db_session = Mock()
    db_session.query.return_value.filter.return_value.first.return_value = job

    with ui_client_factory(db_session) as client:
        response = client.get(f"/jobs/{job.job_id}", headers={"X-API-Key": mock_settings.auth_password})

    assert response.status_code == 200
    assert "Failure Reason" in response.text
    assert "Timeout waiting for host approval" in response.text
    assert "waiting_lobby" in response.text
    assert "Failure Logs" in response.text
    assert "Failure metadata" in response.text
    assert "Browser console log" in response.text
    assert "FFmpeg log" in response.text
    assert "prejoin loaded" in response.text
    assert "ffmpeg version test-build" in response.text


def test_jobs_detail_shows_empty_state_when_no_per_job_logs(ui_client_factory, mock_settings, tmp_path):
    """Failed jobs without readable diagnostics should render a clear empty state."""
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()

    job = _make_job(JobStatus.FAILED.value, str(diagnostics_dir))
    db_session = Mock()
    db_session.query.return_value.filter.return_value.first.return_value = job

    with ui_client_factory(db_session) as client:
        response = client.get(f"/jobs/{job.job_id}", headers={"X-API-Key": mock_settings.auth_password})

    assert response.status_code == 200
    assert "Failure Logs" in response.text
    assert "No per-job logs captured for this failure." in response.text


def test_jobs_detail_hides_failure_logs_for_succeeded_job(ui_client_factory, mock_settings, tmp_path):
    """Successful jobs should not render the failure log section."""
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    (diagnostics_dir / "console.log").write_text("console output", encoding="utf-8")

    job = _make_job(JobStatus.SUCCEEDED.value, str(diagnostics_dir))
    job.error_code = None
    job.error_message = None
    db_session = Mock()
    db_session.query.return_value.filter.return_value.first.return_value = job

    with ui_client_factory(db_session) as client:
        response = client.get(f"/jobs/{job.job_id}", headers={"X-API-Key": mock_settings.auth_password})

    assert response.status_code == 200
    assert "Failure Logs" not in response.text
    assert "Failure Reason" not in response.text


def test_jobs_detail_renders_logs_for_canceled_job(ui_client_factory, mock_settings, tmp_path):
    """Canceled jobs should still expose related per-job logs."""
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    (diagnostics_dir / "console.log").write_text("[warn] stop requested by user", encoding="utf-8")

    job = _make_job(JobStatus.CANCELED.value, str(diagnostics_dir))
    db_session = Mock()
    db_session.query.return_value.filter.return_value.first.return_value = job

    with ui_client_factory(db_session) as client:
        response = client.get(f"/jobs/{job.job_id}", headers={"X-API-Key": mock_settings.auth_password})

    assert response.status_code == 200
    assert "Cancellation Reason" in response.text
    assert "stop requested by user" in response.text


def test_jobs_log_route_serves_allowed_log_and_rejects_unknown_names(ui_client_factory, mock_settings, tmp_path):
    """The log endpoint should serve whitelisted logs only."""
    diagnostics_dir = tmp_path / "diagnostics"
    diagnostics_dir.mkdir()
    (diagnostics_dir / "console.log").write_text("browser console output", encoding="utf-8")

    job = _make_job(JobStatus.FAILED.value, str(diagnostics_dir))
    db_session = Mock()
    db_session.query.return_value.filter.return_value.first.return_value = job

    with ui_client_factory(db_session) as client:
        ok_response = client.get(
            f"/jobs/{job.job_id}/logs/console.log",
            headers={"X-API-Key": mock_settings.auth_password},
        )
        missing_response = client.get(
            f"/jobs/{job.job_id}/logs/app.log",
            headers={"X-API-Key": mock_settings.auth_password},
        )

    assert ok_response.status_code == 200
    assert ok_response.text == "browser console output"
    assert missing_response.status_code == 404
