"""Tests for database models."""

import database.models as models_module
from database.models import (
    DetectionLog,
    ErrorCode,
    JobStatus,
    Meeting,
    RecordingJob,
    Schedule,
    ScheduleType,
    TelegramUser,
)


class TestEnums:
    """Tests for model enums."""

    def test_provider_type_enum_is_not_kept(self):
        """Provider registry is the single provider list, not a model enum."""
        assert not hasattr(models_module, "ProviderType")

    def test_schedule_type_values(self):
        """ScheduleType should have expected values."""
        assert ScheduleType.ONCE.value == "once"
        assert ScheduleType.CRON.value == "cron"

    def test_legacy_duration_mode_enum_is_not_kept(self):
        """Legacy duration_mode values are persisted strings, not a supported enum API."""
        assert not hasattr(models_module, "DurationMode")

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


class TestModelSerializationBoundary:
    """Tests for ORM model serialization ownership."""

    def test_api_models_do_not_expose_generic_to_dict_serializers(self):
        """API responses should use explicit route schemas, not ORM-wide serializers."""
        model_classes = [Meeting, Schedule, RecordingJob, TelegramUser, DetectionLog]

        for model_class in model_classes:
            assert not hasattr(model_class, "to_dict")


class TestScheduleEffectiveMethods:
    """Tests for Schedule.get_effective_* methods."""

    def test_get_effective_meeting_code_with_override(self):
        """Should return override when set."""
        schedule = Schedule(
            meeting=Meeting(meeting_code="default-room"),
            override_meeting_code="override-room",
        )

        assert schedule.get_effective_meeting_code() == "override-room"

    def test_get_effective_meeting_code_without_override(self):
        """Should return meeting default when no override."""
        schedule = Schedule(
            meeting=Meeting(meeting_code="default-room"),
            override_meeting_code=None,
        )

        assert schedule.get_effective_meeting_code() == "default-room"

    def test_get_effective_display_name_with_override(self):
        """Should return override when set."""
        schedule = Schedule(
            meeting=Meeting(default_display_name="Default Bot"),
            override_display_name="Custom Bot",
        )

        assert schedule.get_effective_display_name() == "Custom Bot"

    def test_get_effective_display_name_without_override(self):
        """Should return meeting default when no override."""
        schedule = Schedule(
            meeting=Meeting(default_display_name="Default Bot"),
            override_display_name=None,
        )

        assert schedule.get_effective_display_name() == "Default Bot"


class TestTelegramUserDisplayName:
    """Tests for TelegramUser.display_name property."""

    def test_display_name_with_username(self):
        """Should return @username when username is set."""
        user = TelegramUser(
            username="testuser",
            first_name="Test",
            last_name="User",
            chat_id=12345,
        )

        assert user.display_name == "@testuser"

    def test_display_name_with_first_name_only(self):
        """Should return first_name when no username."""
        user = TelegramUser(
            username=None,
            first_name="John",
            last_name=None,
            chat_id=12345,
        )

        assert user.display_name == "John"

    def test_display_name_with_full_name(self):
        """Should return first_name + last_name when available."""
        user = TelegramUser(
            username=None,
            first_name="John",
            last_name="Doe",
            chat_id=12345,
        )

        assert user.display_name == "John Doe"

    def test_display_name_fallback_to_chat_id(self):
        """Should return 'User {chat_id}' when no name info."""
        user = TelegramUser(
            username=None,
            first_name=None,
            last_name=None,
            chat_id=12345,
        )

        assert user.display_name == "User 12345"
