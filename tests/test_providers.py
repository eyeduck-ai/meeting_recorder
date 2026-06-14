"""Tests for provider modules."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

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
        """Full Zoom launch URL should be converted to the web client."""
        provider = ZoomProvider()
        input_url = "https://zoom.us/j/123456789"
        url = provider.build_join_url(input_url)

        assert "zoom.us/wc/join/123456789" in url
        assert "fromPWA=1" in url
        assert "zc=0" in url

    def test_build_join_url_with_existing_params(self):
        """URL with existing params should preserve them and add web-client params."""
        provider = ZoomProvider()
        input_url = "https://zoom.us/j/123456789?pwd=abc123"
        url = provider.build_join_url(input_url)

        assert "zoom.us/wc/join/123456789" in url
        assert "pwd=abc123" in url
        assert "fromPWA=1" in url
        assert "zc=0" in url

    def test_build_join_url_meeting_number(self):
        """Numeric meeting ID should generate a web-client URL."""
        provider = ZoomProvider()
        url = provider.build_join_url("123456789")

        assert "zoom.us/wc/join/123456789" in url
        assert "fromPWA=1" in url
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

    def __init__(self, count: int = 0, visible: bool | None = None):
        self._count = count
        self._visible = count > 0 if visible is None else visible
        self.clicked = False
        self.filled_value = None
        self.pressed_key = None
        self.wait_for_calls = []

    async def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self

    def nth(self, _index: int):
        return self

    async def wait_for(self, *, state: str = "visible", timeout: int = 3000):
        self.wait_for_calls.append({"state": state, "timeout": timeout})
        if self._count <= 0:
            raise TimeoutError("selector not found")
        if state == "visible" and not self._visible:
            raise TimeoutError("selector not visible")

    async def is_visible(self) -> bool:
        return self._visible

    async def click(self, *_args, **_kwargs):
        self.clicked = True

    async def fill(self, value: str, *_args, **_kwargs):
        self.filled_value = value

    async def press(self, key: str):
        self.pressed_key = key

    async def get_attribute(self, _name: str):
        return None


class FakePage:
    """Minimal Playwright page stub for provider probe tests."""

    def __init__(
        self, counts: dict[str, int] | None = None, *, url: str = "https://example.test/room", title: str = ""
    ):
        self._counts = counts or {}
        self._locators = {}
        self.url = url
        self._title = title

    def locator(self, selector: str) -> FakeLocator:
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self._counts.get(selector, 0))
        return self._locators[selector]

    async def title(self) -> str:
        return self._title


class FakeFrame:
    """Minimal frame locator stub for Webex probe tests."""

    def __init__(self, counts: dict[str, int] | None = None):
        self._counts = counts or {}
        self._locators = {}

    def locator(self, selector: str) -> FakeLocator:
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self._counts.get(selector, 0))
        return self._locators[selector]


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

    @pytest.mark.asyncio
    async def test_wait_for_page_idle_uses_bounded_playwright_wait(self):
        provider = SequenceProvider([])
        page = FakePage()
        page.wait_for_load_state = AsyncMock()

        result = await provider.wait_for_page_idle(page, timeout_ms=1234, fallback_sec=0.1, reason="test")

        assert result is True
        page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=1234)

    @pytest.mark.asyncio
    async def test_wait_for_page_idle_uses_short_fallback_sleep(self, monkeypatch):
        provider = SequenceProvider([])
        page = FakePage()
        page.wait_for_load_state = AsyncMock(side_effect=TimeoutError("not idle"))
        fallback_sleep = AsyncMock()
        monkeypatch.setattr("providers.base.asyncio.sleep", fallback_sleep)

        result = await provider.wait_for_page_idle(page, timeout_ms=1, fallback_sec=0.25, reason="test")

        assert result is False
        fallback_sleep.assert_awaited_once_with(0.25)

    @pytest.mark.asyncio
    async def test_wait_for_any_selector_returns_match_without_fallback_sleep(self, monkeypatch):
        provider = SequenceProvider([])
        page = FakePage({"#ready": 1})
        fallback_sleep = AsyncMock()
        monkeypatch.setattr("providers.base.asyncio.sleep", fallback_sleep)

        match = await provider.wait_for_any_selector(
            page,
            ["#missing", "#ready"],
            timeout_ms=100,
            fallback_sec=0.25,
            reason="test selector",
        )

        assert match is not None
        assert match.selector == "#ready"
        fallback_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wait_for_any_selector_uses_bounded_fallback_when_missing(self, monkeypatch):
        provider = SequenceProvider([])
        page = FakePage()
        fallback_sleep = AsyncMock()
        monkeypatch.setattr("providers.base.asyncio.sleep", fallback_sleep)

        match = await provider.wait_for_any_selector(
            page,
            ["#missing"],
            timeout_ms=100,
            fallback_sec=0.25,
            reason="test selector",
        )

        assert match is None
        fallback_sleep.assert_awaited_once_with(0.25)


class TestProviderBoundedWaitRegressions:
    """Provider wait paths should use bounded helpers instead of direct sleeps."""

    def test_provider_modules_do_not_use_direct_asyncio_sleep(self):
        repo_root = Path(__file__).resolve().parents[1]
        for provider_file in ("providers/jitsi.py", "providers/webex.py", "providers/zoom.py"):
            source = (repo_root / provider_file).read_text(encoding="utf-8")
            assert "asyncio.sleep" not in source

    @pytest.mark.asyncio
    async def test_jitsi_password_dialog_uses_bounded_selector_wait(self):
        provider = JitsiProvider()
        page = FakePage(
            {
                'input[name="lockKey"]': 1,
                'button:has-text("OK")': 1,
            }
        )

        result = await provider.apply_password(page, "secret")

        assert result is True
        assert page.locator('input[name="lockKey"]').filled_value == "secret"
        assert page.locator('button:has-text("OK")').clicked is True

    @pytest.mark.asyncio
    async def test_webex_password_dialog_uses_bounded_selector_wait(self, monkeypatch):
        provider = WebexProvider()
        iframe = FakeFrame(
            {
                'input[type="password"]': 1,
                '[data-test="submit-button"], button:has-text("OK"), button:has-text("Submit")': 1,
            }
        )
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        result = await provider.apply_password(FakePage(), "secret")

        assert result is True
        assert iframe.locator('input[type="password"]').filled_value == "secret"

    @pytest.mark.asyncio
    async def test_zoom_display_name_waits_for_selector_before_fill(self):
        provider = ZoomProvider()
        page = FakePage({"#input-for-name": 1})

        result = await provider._fill_display_name(page, ["#input-for-name"], "Recorder")

        assert result is True
        assert page.locator("#input-for-name").filled_value == "Recorder"


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

    @pytest.mark.asyncio
    async def test_zoom_probe_state_checks_pwa_child_frames(self):
        provider = ZoomProvider()
        page = FakePage(url="https://app.zoom.us/wc/123/join")
        page.frames = [FakeFrame({"#input-for-name": 1})]

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN
        assert "#input-for-name" in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_zoom_probe_state_detects_in_meeting_from_child_frame(self):
        provider = ZoomProvider()
        page = FakePage(url="https://app.zoom.us/wc/123/join")
        page.frames = [FakeFrame({".meeting-app": 1})]

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING
        assert ".meeting-app" in snapshot.evidence["matched_selectors"]
