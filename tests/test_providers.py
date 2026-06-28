"""Tests for provider modules."""

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from providers.base import BaseProvider, DiagnosticData, JoinResult, MeetingState, MeetingStateSnapshot
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider
from providers.zoom import ZoomJoinStage, ZoomProvider


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

    def test_build_join_url_webinar_launch_url(self):
        """Zoom /w launch URLs should also be converted to the web client candidate."""
        provider = ZoomProvider()
        input_url = "https://company.zoom.us/w/123456789?tk=redacted&pwd=abc123&uuid=redacted"
        url = provider.build_join_url(input_url)

        assert "company.zoom.us/wc/join/123456789" in url
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

    def __init__(self, count: int = 0, visible: bool | None = None, *, page=None, selector: str | None = None):
        self._count = count
        self._visible = count > 0 if visible is None else visible
        self._page = page
        self._selector = selector
        self.clicked = False
        self.filled_value = None
        self.pressed_key = None
        self.wait_for_calls = []

    async def count(self) -> int:
        if self._page is not None and self._selector is not None:
            return self._page._counts.get(self._selector, self._count)
        return self._count

    @property
    def first(self):
        return self

    def nth(self, _index: int):
        return self

    async def wait_for(self, *, state: str = "visible", timeout: int = 3000):
        self.wait_for_calls.append({"state": state, "timeout": timeout})
        count = await self.count()
        if count <= 0:
            raise TimeoutError("selector not found")
        if state == "visible" and not await self.is_visible():
            raise TimeoutError("selector not visible")

    async def is_visible(self) -> bool:
        if self._page is not None and self._selector is not None:
            return self._page._counts.get(self._selector, self._count) > 0
        return self._visible

    async def click(self, *_args, **_kwargs):
        self.clicked = True
        if self._page is not None and self._selector in self._page._click_handlers:
            self._page._click_handlers[self._selector](self._page)

    async def fill(self, value: str, *_args, **_kwargs):
        self.filled_value = value
        if self._page is not None and self._selector in self._page._fill_handlers:
            self._page._fill_handlers[self._selector](self._page, value)

    async def press(self, key: str):
        self.pressed_key = key

    async def get_attribute(self, _name: str):
        if self._page is not None and self._selector is not None:
            return self._page._attributes.get((self._selector, _name))
        return None


class FakePage:
    """Minimal Playwright page stub for provider probe tests."""

    def __init__(
        self,
        counts: dict[str, int] | None = None,
        *,
        url: str = "https://example.test/room",
        title: str = "",
        click_handlers: dict | None = None,
        fill_handlers: dict | None = None,
        attributes: dict[tuple[str, str], str] | None = None,
        goto_handler=None,
    ):
        self._counts = counts or {}
        self._locators = {}
        self.url = url
        self._title = title
        self._click_handlers = click_handlers or {}
        self._fill_handlers = fill_handlers or {}
        self._attributes = attributes or {}
        self._goto_handler = goto_handler
        self.goto_calls = []
        self.wait_for_load_state_calls = []

    def locator(self, selector: str) -> FakeLocator:
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self._counts.get(selector, 0), page=self, selector=selector)
        return self._locators[selector]

    async def title(self) -> str:
        return self._title

    async def wait_for_load_state(self, state: str, timeout: int = 3000):
        self.wait_for_load_state_calls.append({"state": state, "timeout": timeout})

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        self.goto_calls.append({"url": url, "wait_until": wait_until})
        self.url = url
        if self._goto_handler:
            self._goto_handler(self, url)


class FakeFrame:
    """Minimal frame locator stub for Webex probe tests."""

    def __init__(self, counts: dict[str, int] | None = None):
        self._counts = counts or {}
        self._locators = {}
        self._click_handlers = {}
        self._fill_handlers = {}
        self._attributes = {}

    def locator(self, selector: str) -> FakeLocator:
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self._counts.get(selector, 0), page=self, selector=selector)
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
    async def test_wait_in_lobby_honors_cancel_callback(self):
        """Active job cancellation should break out of provider lobby waits immediately."""
        provider = SequenceProvider([MeetingStateSnapshot(state=MeetingState.LOBBY)])

        with pytest.raises(asyncio.CancelledError):
            await provider.wait_in_lobby(
                FakePage(),
                max_wait_sec=30,
                cancel_callback=lambda: True,
            )

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
                '[data-test="submit-button"]': 1,
            }
        )
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        result = await provider.apply_password(FakePage({"#unified-webclient-iframe": 1}), "secret")

        assert result is True
        assert iframe.locator('input[type="password"]').filled_value == "secret"

    @pytest.mark.asyncio
    async def test_webex_prejoin_advances_visible_timing_modal_before_filling_name(self, monkeypatch):
        provider = WebexProvider()
        iframe = FakeFrame({'[data-test="join-button"]': 0})

        def open_web_client(_page):
            iframe._counts['[data-test="Name (required)"]'] = 1
            iframe._counts['[data-test="join-button"]'] = 1

        page = FakePage(
            {
                "#unified-webclient-iframe": 1,
                "#fallBkJoinByBrowser:visible": 1,
            },
            click_handlers={"#fallBkJoinByBrowser:visible": open_web_client},
        )
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        await provider.prejoin(page, "Recorder")

        assert page.locator("#fallBkJoinByBrowser:visible").clicked is True
        assert iframe.locator('[data-test="Name (required)"]').filled_value == "Recorder"

    @pytest.mark.asyncio
    async def test_webex_prejoin_fills_localized_mdc_name_input(self, monkeypatch):
        provider = WebexProvider()
        iframe = FakeFrame(
            {
                'mdc-input[data-test="名稱"]': 1,
                'mdc-button:has-text("關閉"):visible': 1,
                '[data-test="join-button"]:not([disabled]):not([aria-disabled="true"])': 1,
            }
        )
        page = FakePage({"#unified-webclient-iframe": 1})
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        await provider.prejoin(page, "Recorder")

        assert iframe.locator('mdc-input[data-test="名稱"]').filled_value == "Recorder"
        assert iframe.locator('mdc-button:has-text("關閉"):visible').clicked is True

    @pytest.mark.asyncio
    async def test_webex_browser_join_prefers_visible_timing_modal_button(self):
        provider = WebexProvider()
        page = FakePage(
            {
                "#fallBkJoinByBrowser:visible": 1,
                'button:has-text("Join from browser"):visible': 1,
            }
        )

        clicked = await provider._click_visible_browser_join(page)

        assert clicked is True
        assert page.locator("#fallBkJoinByBrowser:visible").clicked is True

    @pytest.mark.asyncio
    async def test_webex_prejoin_opens_preloaded_web_client_when_timing_modal_stalls(self, monkeypatch):
        provider = WebexProvider()
        iframe = FakeFrame({})

        browser_prompt_selector = '[role="dialog"]:visible #fallBkJoinByBrowser:has-text("Join from browser")'

        def open_direct_web_client(page, _url):
            page._counts[browser_prompt_selector] = 0
            page._counts["#fallBkJoinByBrowser:visible"] = 0
            page._counts["#broadcom-center-right:visible"] = 0
            page._counts['[data-test="Name (required)"]'] = 1
            page._counts['[data-test="join-button"]'] = 1

        page = FakePage(
            {
                "#unified-webclient-iframe": 1,
                browser_prompt_selector: 1,
                "#fallBkJoinByBrowser:visible": 1,
                "#broadcom-center-right:visible": 1,
            },
            attributes={
                (
                    "#unified-webclient-iframe",
                    "src",
                ): "https://web.webex.com/meeting?mtuuid=abc&preload=true&darkMode=false"
            },
            goto_handler=open_direct_web_client,
        )
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        await provider.prejoin(page, "Recorder")

        assert page.goto_calls
        assert page.goto_calls[0]["url"] == "https://web.webex.com/meeting?mtuuid=abc&darkMode=false&preload=false"
        assert page.locator('[data-test="Name (required)"]').filled_value == "Recorder"

    @pytest.mark.asyncio
    async def test_webex_click_join_rejects_still_visible_browser_prompt(self):
        provider = WebexProvider()
        page = FakePage({'[role="dialog"]:visible #fallBkJoinByBrowser:has-text("Join from browser")': 1})

        with pytest.raises(RuntimeError, match="browser join prompt still visible"):
            await provider.click_join(page)

    @pytest.mark.asyncio
    async def test_webex_prejoin_dialog_dismissal_does_not_click_cookie_reject(self):
        provider = WebexProvider()
        page = FakePage({'button:has-text("Reject"):visible': 1})

        dismissed = await provider._dismiss_prejoin_dialogs(page)

        assert dismissed is False
        assert page.locator('button:has-text("Reject"):visible').clicked is False

    @pytest.mark.asyncio
    async def test_webex_accepts_cookie_banner_inside_iframe(self):
        provider = WebexProvider()
        iframe = FakeFrame({'mdc-button:has-text("Accept"):visible': 1})

        accepted = await provider._accept_cookie_banner(iframe)

        assert accepted is True
        assert iframe.locator('mdc-button:has-text("Accept"):visible').clicked is True

    @pytest.mark.asyncio
    async def test_webex_mutes_currently_unmuted_microphone(self):
        provider = WebexProvider()
        iframe = FakeFrame({'[data-test="microphone-button"]': 1})
        iframe._attributes[('[data-test="microphone-button"]', "aria-label")] = (
            "Microphone is currently unmuted - click to mute"
        )

        muted = await provider._mute_microphone_if_needed(iframe)

        assert muted is True
        assert iframe.locator('[data-test="microphone-button"]').clicked is True

    @pytest.mark.asyncio
    async def test_webex_leaves_currently_muted_microphone_alone(self):
        provider = WebexProvider()
        iframe = FakeFrame({'[data-test="microphone-button"]': 1})
        iframe._attributes[('[data-test="microphone-button"]', "aria-label")] = (
            "Microphone is currently muted - click to unmute"
        )

        muted = await provider._mute_microphone_if_needed(iframe)

        assert muted is False
        assert iframe.locator('[data-test="microphone-button"]').clicked is False

    @pytest.mark.asyncio
    async def test_webex_wait_until_joined_reclicks_when_back_on_prejoin(self):
        provider = WebexProvider()
        page = FakePage()
        provider.probe_state = AsyncMock(
            side_effect=[
                MeetingStateSnapshot(state=MeetingState.PREJOIN),
                MeetingStateSnapshot(state=MeetingState.IN_MEETING),
            ]
        )
        provider.click_join = AsyncMock()
        provider.short_ui_settle = AsyncMock()

        result = await provider.wait_until_joined(page, timeout_sec=5)

        assert result == JoinResult(success=True)
        provider.click_join.assert_awaited_once_with(page)

    @pytest.mark.asyncio
    async def test_zoom_display_name_waits_for_selector_before_fill(self):
        provider = ZoomProvider()
        page = FakePage({"#input-for-name": 1})

        result = await provider._fill_display_name(page, ["#input-for-name"], "Recorder")

        assert result is True
        assert page.locator("#input-for-name").filled_value == "Recorder"

    @pytest.mark.asyncio
    async def test_zoom_prejoin_accepts_cookie_then_clicks_browser_join(self):
        provider = ZoomProvider()
        browser_selector = 'button:has-text("Join from browser")'

        def accept_cookie(page):
            page._counts["#onetrust-accept-btn-handler"] = 0
            page._counts["#onetrust-banner-sdk"] = 0

        def browser_join(page):
            page._counts[browser_selector] = 0
            page._counts["#input-for-name"] = 1
            page._counts["#joinBtn"] = 1
            page.url = "https://zoom.us/wc/join/123?pwd=passcode"

        page = FakePage(
            {
                "#onetrust-accept-btn-handler": 1,
                "#onetrust-banner-sdk": 1,
                browser_selector: 1,
            },
            url="https://zoom.us/w/123?tk=secret&pwd=passcode&uuid=secret#success",
            click_handlers={
                "#onetrust-accept-btn-handler": accept_cookie,
                browser_selector: browser_join,
            },
        )

        await provider.prejoin(page, "Recorder", password=None)

        assert page.locator("#onetrust-accept-btn-handler").clicked is True
        assert page.locator(browser_selector).clicked is True
        assert page.locator("#input-for-name").filled_value == "Recorder"

    @pytest.mark.asyncio
    async def test_zoom_password_form_uses_url_pwd_when_request_password_omitted(self):
        provider = ZoomProvider()
        page = FakePage(
            {
                "#inputpasscode": 1,
                'button:has-text("Join")': 1,
            },
            url="https://zoom.us/wc/join/123?pwd=url-passcode",
        )

        state = await provider._detect_zoom_state(page)
        result = await provider._advance_zoom_state(
            page,
            state,
            display_name="Recorder",
            password=None,
            click_final_join=True,
        )

        assert result is True
        assert page.locator("#inputpasscode").filled_value == "url-passcode"
        assert page.locator('button:has-text("Join")').clicked is True

    @pytest.mark.asyncio
    async def test_zoom_dismisses_hardware_acceleration_toast(self):
        provider = ZoomProvider()
        close_selector = 'button[aria-label*="close" i]'
        page = FakePage(
            {
                ':text("hardware acceleration")': 1,
                close_selector: 1,
            },
            url="https://app.zoom.us/wc/123/join",
        )

        dismissed = await provider.dismiss_transient_overlays(page)

        assert dismissed is True
        assert page.locator(close_selector).clicked is True

    @pytest.mark.asyncio
    async def test_zoom_overlay_dismissal_does_not_click_close_without_target_notice(self):
        provider = ZoomProvider()
        close_selector = 'button[aria-label*="close" i]'
        page = FakePage({close_selector: 1}, url="https://app.zoom.us/wc/123/join")

        dismissed = await provider.dismiss_transient_overlays(page)

        assert dismissed is False
        assert page.locator(close_selector).clicked is False


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
        page = FakePage({"#unified-webclient-iframe": 1}, url="https://company.webex.com/meet/test", title="")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _: FakeFrame({'[data-test="grid-layout"]': 1}))

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING
        assert '[data-test="grid-layout"]' in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_webex_probe_state_does_not_treat_prejoin_local_stream_as_lobby(self, monkeypatch):
        provider = WebexProvider()
        page = FakePage({"#unified-webclient-iframe": 1}, url="https://company.webex.com/meet/test", title="Webex")
        monkeypatch.setattr(
            provider,
            "_get_webex_iframe",
            lambda _: FakeFrame(
                {
                    '[data-test="local_stream"]': 1,
                    '[data-test="join-button"]': 1,
                    'mdc-input[data-test*="name" i]': 1,
                }
            ),
        )

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN
        assert '[data-test="join-button"]' in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_webex_probe_state_keeps_connecting_ui_as_joining(self, monkeypatch):
        provider = WebexProvider()
        page = FakePage({"#unified-webclient-iframe": 1}, url="https://company.webex.com/meet/test", title="準備加入")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _: FakeFrame({':text("正在連線")': 1}))

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.JOINING
        assert ':text("正在連線")' in snapshot.evidence["matched_selectors"]

    @pytest.mark.asyncio
    async def test_webex_probe_state_does_not_treat_connecting_controls_as_joined(self, monkeypatch):
        provider = WebexProvider()
        page = FakePage({"#unified-webclient-iframe": 1}, url="https://company.webex.com/meet/test", title="Webex")
        monkeypatch.setattr(
            provider,
            "_get_webex_iframe",
            lambda _: FakeFrame(
                {
                    ':text("正在連線")': 1,
                    '[data-test="participants-toggle-button"]': 1,
                }
            ),
        )

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.JOINING
        assert ':text("正在連線")' in snapshot.evidence["matched_selectors"]

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

    @pytest.mark.asyncio
    async def test_zoom_probe_state_treats_success_launch_url_as_browser_join_prejoin(self):
        provider = ZoomProvider()
        page = FakePage(
            {'button:has-text("Join from browser")': 1},
            url="https://zoom.us/w/123?tk=secret-token&pwd=secret-pass&uuid=secret-uuid#success",
        )

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN
        assert snapshot.evidence["zoom_stage"] == ZoomJoinStage.BROWSER_JOIN_AVAILABLE.value
        assert snapshot.evidence["url"] == "https://zoom.us/w/123"
        evidence_text = str(snapshot.evidence)
        assert "tk=" not in evidence_text
        assert "pwd=" not in evidence_text
        assert "uuid=" not in evidence_text
        assert "secret-token" not in evidence_text
        assert "secret-pass" not in evidence_text

    @pytest.mark.asyncio
    async def test_zoom_probe_state_classifies_app_zoom_web_client_url(self):
        provider = ZoomProvider()
        page = FakePage({".meeting-app": 1}, url="https://app.zoom.us/wc/123/join?pwd=secret")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING
        assert snapshot.evidence["url"] == "https://app.zoom.us/wc/123/join"
        assert snapshot.evidence["url_kind"] == "web_client"
