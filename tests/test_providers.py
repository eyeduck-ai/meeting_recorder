"""Tests for provider modules."""

from datetime import datetime
from pathlib import Path

import pytest

from providers.base import BaseProvider, DiagnosticData, JoinResult, MeetingState, MeetingStateSnapshot
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider
from providers.zoom import ZoomProvider


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


class TestZoomProvider:
    """Tests for ZoomProvider."""

    def test_build_join_url_full_url(self):
        """Full URL should have zc=0 added."""
        provider = ZoomProvider()
        input_url = "https://zoom.us/j/123456789"
        url = provider.build_join_url(input_url)

        assert "123456789" in url
        assert "zc=0" in url

    def test_build_join_url_with_existing_params(self):
        """URL with existing params should preserve them and add zc=0."""
        provider = ZoomProvider()
        input_url = "https://zoom.us/j/123456789?pwd=abc123"
        url = provider.build_join_url(input_url)

        assert "pwd=abc123" in url
        assert "zc=0" in url

    def test_build_join_url_meeting_number(self):
        """Numeric meeting ID should generate proper URL."""
        provider = ZoomProvider()
        url = provider.build_join_url("123456789")

        assert "zoom.us/j/123456789" in url
        assert "zc=0" in url

    def test_build_join_url_meeting_number_with_dashes(self):
        """Meeting ID with dashes should be cleaned."""
        provider = ZoomProvider()
        url = provider.build_join_url("123-456-789")

        assert "123456789" in url
        assert "zc=0" in url

    def test_build_join_url_custom_base(self):
        """Custom base URL should be used."""
        provider = ZoomProvider()
        url = provider.build_join_url("987654321", "https://company.zoom.us")

        assert "company.zoom.us" in url
        assert "987654321" in url

    def test_name_property(self):
        """Provider name should be 'zoom'."""
        provider = ZoomProvider()
        assert provider.name == "zoom"


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


class FakeLocator:
    """Minimal async locator stub for provider probe tests."""

    def __init__(self, count: int = 0):
        self._count = count

    async def count(self) -> int:
        return self._count


class FakePage:
    """Minimal Playwright page stub for provider probe tests."""

    def __init__(
        self, counts: dict[str, int] | None = None, *, url: str = "https://example.test/room", title: str = ""
    ):
        self._counts = counts or {}
        self.url = url
        self._title = title

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._counts.get(selector, 0))

    async def title(self) -> str:
        return self._title


class FakeFrame:
    """Minimal frame locator stub for Webex probe tests."""

    def __init__(self, counts: dict[str, int] | None = None):
        self._counts = counts or {}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._counts.get(selector, 0))


class SequenceProvider(BaseProvider):
    """Small provider used to exercise the generic BaseProvider state machine."""

    def __init__(self, snapshots: list[MeetingStateSnapshot]):
        self._snapshots = list(snapshots)
        self.password_attempts = 0

    @property
    def name(self) -> str:
        return "sequence"

    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        return "https://example.test/room"

    async def prejoin(self, page, display_name: str, password: str | None = None) -> None:
        return None

    async def click_join(self, page) -> None:
        return None

    async def apply_password(self, page, password: str) -> bool:
        self.password_attempts += 1
        return True

    async def probe_state(self, page) -> MeetingStateSnapshot:
        if self._snapshots:
            return self._snapshots.pop(0)
        return MeetingStateSnapshot(state=MeetingState.JOINING, confidence=0.5)

    async def set_layout(self, page, preset: str = "speaker") -> bool:
        return True


class TestProviderStateMachine:
    """Tests for the shared BaseProvider state-machine helpers."""

    @pytest.mark.asyncio
    async def test_wait_until_joined_retries_password_prompt(self):
        """The generic join wait should retry through a password prompt and keep the provider contract stable."""
        provider = SequenceProvider(
            [
                MeetingStateSnapshot(
                    state=MeetingState.ERROR,
                    error_code="PASSWORD_REQUIRED",
                    evidence={"password_prompt": True},
                ),
                MeetingStateSnapshot(state=MeetingState.IN_MEETING),
            ]
        )

        result = await provider.wait_until_joined(FakePage(), timeout_sec=5, password="secret")

        assert result == JoinResult(success=True)
        assert provider.password_attempts == 1


class TestProviderProbeState:
    """Tests for provider-specific probe_state implementations."""

    @pytest.mark.asyncio
    async def test_jitsi_probe_state_detects_in_meeting(self):
        provider = JitsiProvider()
        page = FakePage({"#remoteVideos": 1}, url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING
        assert "#remoteVideos" in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_jitsi_probe_state_detects_password_prompt(self):
        provider = JitsiProvider()
        page = FakePage({'input[name="lockKey"]': 1}, url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ERROR
        assert snapshot.error_code == "PASSWORD_REQUIRED"
        assert snapshot.evidence["password_prompt"] is True

    @pytest.mark.asyncio
    async def test_webex_probe_state_detects_lobby_from_title(self, monkeypatch):
        provider = WebexProvider()
        page = FakePage(url="https://company.webex.com/meet/test", title="In Lobby")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _: FakeFrame())

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.LOBBY
        assert "title:lobby" in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_webex_probe_state_detects_in_meeting_from_iframe(self, monkeypatch):
        provider = WebexProvider()
        page = FakePage(url="https://company.webex.com/meet/test", title="")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _: FakeFrame({'[data-test="grid-layout"]': 1}))

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING
        assert '[data-test="grid-layout"]' in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_zoom_probe_state_detects_meeting_end(self):
        provider = ZoomProvider()
        page = FakePage({'text="Meeting has ended"': 1}, url="https://zoom.us/wc/test")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ENDED
        assert snapshot.error_code == "MEETING_ENDED"
