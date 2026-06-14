"""Tests for authentication module."""

import time
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import (
    AuthMiddleware,
    _generate_session_token,
    _verify_session_token,
    create_session_token,
    is_authenticated,
    is_public_path,
)
from api.cors import configure_cors, parse_cors_allowed_origins


class TestGenerateSessionToken:
    """Tests for _generate_session_token function."""

    def test_returns_hex_string(self):
        """Token should be a 64-char hex string (SHA256)."""
        token = _generate_session_token("password", "secret", 1234567890)
        assert isinstance(token, str)
        assert len(token) == 64
        # Should be valid hex
        int(token, 16)

    def test_deterministic_output(self):
        """Same inputs should produce same output."""
        t1 = _generate_session_token("pass", "secret", 1000)
        t2 = _generate_session_token("pass", "secret", 1000)
        assert t1 == t2

    def test_different_password_different_token(self):
        """Different passwords should produce different tokens."""
        t1 = _generate_session_token("pass1", "secret", 1000)
        t2 = _generate_session_token("pass2", "secret", 1000)
        assert t1 != t2

    def test_different_secret_different_token(self):
        """Different secrets should produce different tokens."""
        t1 = _generate_session_token("pass", "secret1", 1000)
        t2 = _generate_session_token("pass", "secret2", 1000)
        assert t1 != t2

    def test_different_timestamp_different_token(self):
        """Different timestamps should produce different tokens."""
        t1 = _generate_session_token("pass", "secret", 1000)
        t2 = _generate_session_token("pass", "secret", 2000)
        assert t1 != t2


class TestVerifySessionToken:
    """Tests for _verify_session_token function."""

    def test_valid_token(self, mock_settings):
        """Valid token should return True."""
        # Generate a token with current timestamp
        timestamp = int(time.time())
        signature = _generate_session_token(mock_settings.auth_password, mock_settings.auth_session_secret, timestamp)
        token = f"{timestamp}:{signature}"

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = _verify_session_token(token, mock_settings.auth_session_secret, max_age=86400)
            assert result is True

    def test_expired_token(self, mock_settings):
        """Expired token should return False."""
        # Generate a token from the past
        old_timestamp = int(time.time()) - 100000  # ~27 hours ago
        signature = _generate_session_token(
            mock_settings.auth_password, mock_settings.auth_session_secret, old_timestamp
        )
        token = f"{old_timestamp}:{signature}"

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = _verify_session_token(
                token,
                mock_settings.auth_session_secret,
                max_age=86400,  # 24 hours
            )
            assert result is False

    def test_invalid_signature(self, mock_settings):
        """Token with wrong signature should return False."""
        timestamp = int(time.time())
        token = f"{timestamp}:invalid_signature"

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = _verify_session_token(token, mock_settings.auth_session_secret, max_age=86400)
            assert result is False

    def test_malformed_token_no_colon(self, mock_settings):
        """Token without colon separator should return False."""
        with patch("api.auth.get_settings", return_value=mock_settings):
            result = _verify_session_token("malformed_token", mock_settings.auth_session_secret, max_age=86400)
            assert result is False

    def test_malformed_token_invalid_timestamp(self, mock_settings):
        """Token with non-integer timestamp should return False."""
        with patch("api.auth.get_settings", return_value=mock_settings):
            result = _verify_session_token("not_a_number:signature", mock_settings.auth_session_secret, max_age=86400)
            assert result is False


class TestCreateSessionToken:
    """Tests for create_session_token function."""

    def test_creates_valid_format(self, mock_settings):
        """Created token should have timestamp:signature format."""
        with patch("api.auth.get_settings", return_value=mock_settings):
            token = create_session_token()

        parts = token.split(":")
        assert len(parts) == 2
        # First part should be valid timestamp
        timestamp = int(parts[0])
        assert timestamp > 0
        # Second part should be 64-char hex
        assert len(parts[1]) == 64

    def test_token_is_verifiable(self, mock_settings):
        """Created token should be verifiable."""
        with patch("api.auth.get_settings", return_value=mock_settings):
            token = create_session_token()
            result = _verify_session_token(token, mock_settings.auth_session_secret, max_age=86400)
            assert result is True


class TestIsAuthenticated:
    """Tests for is_authenticated function."""

    def test_no_password_configured(self):
        """Should return True when no password is configured."""
        settings = Mock()
        settings.auth_password = None

        request = Mock()

        with patch("api.auth.get_settings", return_value=settings):
            result = is_authenticated(request)
            assert result is True

    def test_valid_session_cookie(self, mock_settings):
        """Should return True with valid session cookie."""
        # Create a valid token
        timestamp = int(time.time())
        signature = _generate_session_token(mock_settings.auth_password, mock_settings.auth_session_secret, timestamp)
        valid_token = f"{timestamp}:{signature}"

        request = Mock()
        request.cookies = {"session": valid_token}
        request.headers = {}

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = is_authenticated(request)
            assert result is True

    def test_expired_session_cookie(self, mock_settings):
        """Should return False with expired session cookie."""
        # Create an expired token
        old_timestamp = int(time.time()) - 100000
        signature = _generate_session_token(
            mock_settings.auth_password, mock_settings.auth_session_secret, old_timestamp
        )
        expired_token = f"{old_timestamp}:{signature}"

        request = Mock()
        request.cookies = {"session": expired_token}
        request.headers = {}

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = is_authenticated(request)
            assert result is False

    def test_valid_api_key(self, mock_settings):
        """Should return True with valid X-API-Key header."""
        request = Mock()
        request.cookies = {}
        request.headers = {"X-API-Key": mock_settings.auth_password}

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = is_authenticated(request)
            assert result is True

    def test_invalid_api_key(self, mock_settings):
        """Should return False with invalid X-API-Key header."""
        request = Mock()
        request.cookies = {}
        request.headers = {"X-API-Key": "wrong-password"}

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = is_authenticated(request)
            assert result is False

    def test_no_credentials(self, mock_settings):
        """Should return False with no cookie or API key."""
        request = Mock()
        request.cookies = {}
        request.headers = {}

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = is_authenticated(request)
            assert result is False

    def test_cookie_takes_precedence(self, mock_settings):
        """Valid cookie should authenticate even with invalid API key."""
        # Create a valid token
        timestamp = int(time.time())
        signature = _generate_session_token(mock_settings.auth_password, mock_settings.auth_session_secret, timestamp)
        valid_token = f"{timestamp}:{signature}"

        request = Mock()
        request.cookies = {"session": valid_token}
        request.headers = {"X-API-Key": "wrong-password"}

        with patch("api.auth.get_settings", return_value=mock_settings):
            result = is_authenticated(request)
            assert result is True


class TestPublicPaths:
    """Tests for public path matching."""

    def test_public_paths_are_narrow(self):
        assert is_public_path("/health") is True
        assert is_public_path("/api") is True
        assert is_public_path("/api/environment") is True
        assert is_public_path("/login") is True
        assert is_public_path("/static/app.css") is True

        assert is_public_path("/api/v1/jobs/current") is False
        assert is_public_path("/api/detection/config") is False
        assert is_public_path("/api/recordings/list") is False
        assert is_public_path("/api/private") is False
        assert is_public_path("/staticfiles/app.css") is False


class TestAuthMiddleware:
    """Integration tests for auth middleware routing behavior."""

    def _client(self):
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/health")
        async def health():
            return {"ok": True}

        @app.get("/api")
        async def api_info():
            return {"ok": True}

        @app.get("/api/environment")
        async def api_environment():
            return {"ok": True}

        @app.get("/api/v1/jobs/current")
        async def current_job():
            return {"ok": True}

        @app.get("/api/detection/config")
        async def detection_config():
            return {"ok": True}

        @app.get("/api/recordings/list")
        async def recordings_list():
            return {"ok": True}

        @app.get("/static/app.css")
        async def static_asset():
            return {"ok": True}

        @app.get("/dashboard")
        async def dashboard():
            return {"ok": True}

        return TestClient(app)

    def test_public_api_endpoints_remain_accessible(self, mock_settings):
        client = self._client()

        with patch("api.auth.get_settings", return_value=mock_settings):
            assert client.get("/api").status_code == 200
            assert client.get("/api/environment").status_code == 200
            assert client.get("/health").status_code == 200

    def test_protected_api_endpoints_require_auth(self, mock_settings):
        client = self._client()

        with patch("api.auth.get_settings", return_value=mock_settings):
            for path in ("/api/v1/jobs/current", "/api/detection/config", "/api/recordings/list"):
                response = client.get(path)
                assert response.status_code == 401
                assert response.json() == {"detail": "Authentication required"}

    def test_protected_api_endpoints_allow_valid_api_key(self, mock_settings):
        client = self._client()

        with patch("api.auth.get_settings", return_value=mock_settings):
            for path in ("/api/v1/jobs/current", "/api/detection/config", "/api/recordings/list"):
                response = client.get(path, headers={"X-API-Key": mock_settings.auth_password})
                assert response.status_code == 200
                assert response.json() == {"ok": True}

    def test_static_public_and_html_redirect_behavior(self, mock_settings):
        client = self._client()

        with patch("api.auth.get_settings", return_value=mock_settings):
            assert client.get("/static/app.css").status_code == 200

            response = client.get("/dashboard", follow_redirects=False)
            assert response.status_code == 302
            assert response.headers["location"] == "/login?next=/dashboard"


class TestCorsConfiguration:
    """Tests for explicit CORS configuration."""

    def test_parse_cors_allowed_origins(self):
        assert parse_cors_allowed_origins("") == []
        assert parse_cors_allowed_origins(" https://a.example,https://b.example ") == [
            "https://a.example",
            "https://b.example",
        ]

    def test_parse_cors_allowed_origins_rejects_wildcard(self):
        import pytest

        with pytest.raises(ValueError, match="explicit origins"):
            parse_cors_allowed_origins("*")

    def test_cors_is_not_permissive_by_default(self):
        app = FastAPI()
        configure_cors(app, "")

        @app.get("/")
        async def root():
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/", headers={"Origin": "https://admin.example.com"})

        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    def test_cors_allows_only_configured_origins(self):
        app = FastAPI()
        configure_cors(app, "https://admin.example.com,https://ops.example.com")

        @app.get("/")
        async def root():
            return {"ok": True}

        client = TestClient(app)

        allowed = client.get("/", headers={"Origin": "https://admin.example.com"})
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://admin.example.com"
        assert allowed.headers["access-control-allow-credentials"] == "true"

        denied = client.get("/", headers={"Origin": "https://evil.example.com"})
        assert denied.status_code == 200
        assert "access-control-allow-origin" not in denied.headers

    def test_cors_wraps_auth_for_preflight_and_401(self, mock_settings):
        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        configure_cors(app, "https://admin.example.com")

        @app.get("/api/v1/jobs/current")
        async def current_job():
            return {"ok": True}

        client = TestClient(app)

        with patch("api.auth.get_settings", return_value=mock_settings):
            preflight = client.options(
                "/api/v1/jobs/current",
                headers={
                    "Origin": "https://admin.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == "https://admin.example.com"

            unauthorized = client.get(
                "/api/v1/jobs/current",
                headers={"Origin": "https://admin.example.com"},
            )
            assert unauthorized.status_code == 401
            assert unauthorized.headers["access-control-allow-origin"] == "https://admin.example.com"
