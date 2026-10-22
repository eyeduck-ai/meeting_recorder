"""Pytest configuration and shared fixtures."""

import atexit
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

# Test collection must never inherit a developer's live .env database or credentials.
_test_runtime = tempfile.TemporaryDirectory(prefix="meeting-recorder-tests-")
atexit.register(_test_runtime.cleanup)
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_test_runtime.name) / 'app.db'}"
for _directory in ("DATA_DIR", "RECORDINGS_DIR", "DIAGNOSTICS_DIR", "LOGS_DIR"):
    _path = Path(_test_runtime.name) / _directory.lower()
    _path.mkdir()
    os.environ[_directory] = str(_path)
for _secret in ("TELEGRAM_BOT_TOKEN", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"):
    os.environ[_secret] = ""


@pytest.fixture
def mock_settings():
    """Mock Settings object for testing."""
    settings = Mock()
    settings.auth_password = "test-password"
    settings.auth_session_secret = "test-secret"
    settings.auth_session_max_age = 86400
    settings.jitsi_base_url = "https://meet.jit.si/"
    settings.resolution_w = 1920
    settings.resolution_h = 1080
    settings.youtube_client_id = None
    settings.youtube_client_secret = None
    settings.timezone = "UTC"
    return settings


@pytest.fixture
def mock_db_session():
    """Mock SQLAlchemy Session for testing."""
    return Mock()


@pytest.fixture
def mock_meeting():
    """Mock Meeting object for testing."""
    meeting = Mock()
    meeting.id = 1
    meeting.name = "Test Meeting"
    meeting.provider = "jitsi"
    meeting.meeting_code = "test-room"
    meeting.default_display_name = "Recorder Bot"
    meeting.default_password = None
    return meeting


@pytest.fixture
def mock_schedule(mock_meeting):
    """Mock Schedule object for testing."""
    schedule = Mock()
    schedule.id = 1
    schedule.meeting_id = 1
    schedule.meeting = mock_meeting
    schedule.override_meeting_code = None
    schedule.override_display_name = None
    schedule.override_password = None
    schedule.enabled = True
    return schedule
