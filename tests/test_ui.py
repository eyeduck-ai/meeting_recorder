"""UI smoke tests for template rendering and auth flow."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import auth
from api.routes import health, ui


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
    monkeypatch.setattr(ui, "settings", fake_settings)

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

    api_response = client.get("/api")
    assert api_response.status_code == 200
    assert api_response.json()["status"] == "running"


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
