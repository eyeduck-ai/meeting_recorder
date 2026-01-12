"""Tests for authentication module."""

import time
from unittest.mock import Mock, patch

from api.auth import (
    _generate_session_token,
    _verify_session_token,
    create_session_token,
    is_authenticated,
)


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
