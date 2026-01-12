"""Tests for provider modules."""

from datetime import datetime
from pathlib import Path

from providers.base import DiagnosticData
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider


class TestJitsiProvider:
    """Tests for JitsiProvider."""

    def test_build_join_url_basic(self):
        """Basic URL construction with base_url ending in slash."""
        provider = JitsiProvider()
        url = provider.build_join_url("my-room", "https://meet.jit.si/")

        assert url.startswith("https://meet.jit.si/my-room")
        assert "config.startWithVideoMuted=true" in url
        assert "config.startWithAudioMuted=true" in url

    def test_build_join_url_no_trailing_slash(self):
        """URL construction when base_url has no trailing slash."""
        provider = JitsiProvider()
        url = provider.build_join_url("room", "https://meet.jit.si")

        assert "meet.jit.si/room" in url
        assert "#config" in url

    def test_build_join_url_custom_base(self):
        """URL construction with custom Jitsi instance."""
        provider = JitsiProvider()
        url = provider.build_join_url("meeting", "https://jitsi.example.com/")

        assert url.startswith("https://jitsi.example.com/meeting")

    def test_build_join_url_special_characters(self):
        """URL construction with room name containing special chars."""
        provider = JitsiProvider()
        url = provider.build_join_url("my-test-room-123", "https://meet.jit.si/")

        assert "my-test-room-123" in url

    def test_name_property(self):
        """Provider name should be 'jitsi'."""
        provider = JitsiProvider()
        assert provider.name == "jitsi"


class TestWebexProvider:
    """Tests for WebexProvider."""

    def test_build_join_url_full_url(self):
        """Full URL should be returned as-is."""
        provider = WebexProvider()
        input_url = "https://company.webex.com/meet/username"
        url = provider.build_join_url(input_url)

        assert url == input_url

    def test_build_join_url_https_prefix(self):
        """Any URL starting with http should be returned as-is."""
        provider = WebexProvider()
        input_url = "http://webex.com/meet/test"
        url = provider.build_join_url(input_url)

        assert url == input_url

    def test_build_join_url_meeting_number(self):
        """Meeting number should generate j.php URL."""
        provider = WebexProvider()
        url = provider.build_join_url("123456789")

        assert "j.php?MTID=123456789" in url

    def test_build_join_url_meeting_number_with_base(self):
        """Meeting number with custom base URL."""
        provider = WebexProvider()
        url = provider.build_join_url("987654321", "https://company.webex.com")

        assert "company.webex.com" in url
        assert "MTID=987654321" in url

    def test_build_join_url_personal_room(self):
        """Personal room name should generate meet/ URL."""
        provider = WebexProvider()
        url = provider.build_join_url("john.doe", "https://company.webex.com")

        assert "company.webex.com" in url
        assert "meet/john.doe" in url

    def test_build_join_url_default_base(self):
        """Without base_url, should use default webex.com."""
        provider = WebexProvider()
        url = provider.build_join_url("my-room")

        assert "webex.com" in url
        assert "my-room" in url

    def test_name_property(self):
        """Provider name should be 'webex'."""
        provider = WebexProvider()
        assert provider.name == "webex"


class TestDiagnosticData:
    """Tests for DiagnosticData dataclass."""

    def test_to_dict_basic(self):
        """Basic serialization to dict."""
        data = DiagnosticData(
            error_message="Failed to join meeting",
            screenshot_path=None,
            html_path=None,
            console_log_path=None,
            metadata_path=None,
            collected_at=datetime(2024, 1, 15, 10, 30, 0),
        )

        result = data.to_dict()

        assert result["error_message"] == "Failed to join meeting"
        assert result["screenshot_path"] is None
        assert "2024-01-15" in result["collected_at"]

    def test_to_dict_with_paths(self):
        """Serialization with Path objects."""
        data = DiagnosticData(
            output_dir=Path("/app/diagnostics/123"),
            error_message="FFmpeg error",
            screenshot_path=Path("/app/diagnostics/123/screenshot.png"),
            html_path=Path("/app/diagnostics/123/page.html"),
            console_log_path=Path("/app/diagnostics/123/console.log"),
            metadata_path=Path("/app/diagnostics/123/metadata.json"),
            collected_at=datetime(2024, 1, 15, 10, 30, 0),
        )

        result = data.to_dict()

        # Check paths are converted to strings (path separators may vary by OS)
        assert "123" in result["output_dir"]
        assert "screenshot.png" in result["screenshot_path"]
        assert "page.html" in result["html_path"]
        assert "console.log" in result["console_log_path"]

    def test_to_dict_datetime_format(self):
        """Datetime should be serialized to ISO format."""
        data = DiagnosticData(
            error_message="Test error",
            collected_at=datetime(2024, 6, 15, 14, 30, 45),
        )

        result = data.to_dict()

        assert result["collected_at"] == "2024-06-15T14:30:45"
