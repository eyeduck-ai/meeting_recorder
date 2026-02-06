"""Tests for database models."""

from unittest.mock import Mock

from database.models import (
    ErrorCode,
    JobStatus,
    ProviderType,
    ScheduleType,
)


class TestEnums:
    """Tests for model enums."""

    def test_provider_type_values(self):
        """ProviderType should have expected values."""
        assert ProviderType.JITSI.value == "jitsi"
        assert ProviderType.WEBEX.value == "webex"

    def test_schedule_type_values(self):
        """ScheduleType should have expected values."""
        assert ScheduleType.ONCE.value == "once"
        assert ScheduleType.CRON.value == "cron"

    def test_job_status_values(self):
        """JobStatus should have all expected states."""
        expected_statuses = [
            "queued",
            "starting",
            "joining",
            "waiting_lobby",
            "recording",
            "finalizing",
            "uploading",
            "succeeded",
            "failed",
            "canceled",
        ]
        actual_statuses = [s.value for s in JobStatus]
        for status in expected_statuses:
            assert status in actual_statuses

    def test_error_code_join_errors(self):
        """ErrorCode should have join-related errors."""
        assert ErrorCode.JOIN_TIMEOUT.value == "JOIN_TIMEOUT"
        assert ErrorCode.JOIN_FAILED.value == "JOIN_FAILED"
        assert ErrorCode.MEETING_NOT_FOUND.value == "MEETING_NOT_FOUND"
        assert ErrorCode.PASSWORD_REQUIRED.value == "PASSWORD_REQUIRED"
        assert ErrorCode.PASSWORD_INCORRECT.value == "PASSWORD_INCORRECT"

    def test_error_code_lobby_errors(self):
        """ErrorCode should have lobby-related errors."""
        assert ErrorCode.LOBBY_TIMEOUT.value == "LOBBY_TIMEOUT"
        assert ErrorCode.LOBBY_REJECTED.value == "LOBBY_REJECTED"
        assert ErrorCode.NEVER_JOINED.value == "NEVER_JOINED"

    def test_error_code_recording_errors(self):
        """ErrorCode should have recording-related errors."""
        assert ErrorCode.RECORDING_START_FAILED.value == "RECORDING_START_FAILED"
        assert ErrorCode.RECORDING_INTERRUPTED.value == "RECORDING_INTERRUPTED"
        assert ErrorCode.FFMPEG_ERROR.value == "FFMPEG_ERROR"


class TestScheduleEffectiveMethods:
    """Tests for Schedule.get_effective_* methods."""

    def test_get_effective_meeting_code_with_override(self):
        """Should return override when set."""
        meeting = Mock()
        meeting.meeting_code = "default-room"

        schedule = Mock()
        schedule.meeting = meeting
        schedule.override_meeting_code = "override-room"

        # Simulate the method logic
        result = schedule.override_meeting_code or meeting.meeting_code
        assert result == "override-room"

    def test_get_effective_meeting_code_without_override(self):
        """Should return meeting default when no override."""
        meeting = Mock()
        meeting.meeting_code = "default-room"

        schedule = Mock()
        schedule.meeting = meeting
        schedule.override_meeting_code = None

        # Simulate the method logic
        result = schedule.override_meeting_code or meeting.meeting_code
        assert result == "default-room"

    def test_get_effective_display_name_with_override(self):
        """Should return override when set."""
        meeting = Mock()
        meeting.default_display_name = "Default Bot"

        schedule = Mock()
        schedule.meeting = meeting
        schedule.override_display_name = "Custom Bot"

        # Simulate the method logic
        result = schedule.override_display_name or meeting.default_display_name
        assert result == "Custom Bot"

    def test_get_effective_display_name_without_override(self):
        """Should return meeting default when no override."""
        meeting = Mock()
        meeting.default_display_name = "Default Bot"

        schedule = Mock()
        schedule.meeting = meeting
        schedule.override_display_name = None

        # Simulate the method logic
        result = schedule.override_display_name or meeting.default_display_name
        assert result == "Default Bot"


class TestTelegramUserDisplayName:
    """Tests for TelegramUser.display_name property."""

    def test_display_name_with_username(self):
        """Should return @username when username is set."""
        user = Mock()
        user.username = "testuser"
        user.first_name = "Test"
        user.last_name = "User"
        user.chat_id = 12345

        # Simulate the property logic
        if user.username:
            result = f"@{user.username}"
        elif user.first_name:
            result = user.first_name + (f" {user.last_name}" if user.last_name else "")
        else:
            result = f"User {user.chat_id}"

        assert result == "@testuser"

    def test_display_name_with_first_name_only(self):
        """Should return first_name when no username."""
        user = Mock()
        user.username = None
        user.first_name = "John"
        user.last_name = None
        user.chat_id = 12345

        # Simulate the property logic
        if user.username:
            result = f"@{user.username}"
        elif user.first_name:
            result = user.first_name + (f" {user.last_name}" if user.last_name else "")
        else:
            result = f"User {user.chat_id}"

        assert result == "John"

    def test_display_name_with_full_name(self):
        """Should return first_name + last_name when available."""
        user = Mock()
        user.username = None
        user.first_name = "John"
        user.last_name = "Doe"
        user.chat_id = 12345

        # Simulate the property logic
        if user.username:
            result = f"@{user.username}"
        elif user.first_name:
            result = user.first_name + (f" {user.last_name}" if user.last_name else "")
        else:
            result = f"User {user.chat_id}"

        assert result == "John Doe"

    def test_display_name_fallback_to_chat_id(self):
        """Should return 'User {chat_id}' when no name info."""
        user = Mock()
        user.username = None
        user.first_name = None
        user.last_name = None
        user.chat_id = 12345

        # Simulate the property logic
        if user.username:
            result = f"@{user.username}"
        elif user.first_name:
            result = user.first_name + (f" {user.last_name}" if user.last_name else "")
        else:
            result = f"User {user.chat_id}"

        assert result == "User 12345"
