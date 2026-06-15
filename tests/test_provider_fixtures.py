"""Regression tests against saved provider HTML fixtures."""

from pathlib import Path

import pytest

from providers.base import MeetingState
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider
from providers.zoom import ZoomProvider
from tests.fixture_pages import FixturePage

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "providers"


def load_fixture(provider: str, name: str, suffix: str = "html", *, title: str = "", url: str = "https://example.test"):
    """Load a provider fixture into a lightweight page shim."""
    path = FIXTURE_ROOT / provider / f"{name}.{suffix}"
    return FixturePage.from_file(path, title=title, url=url)


class TestJitsiProviderFixtures:
    """Regression tests for Jitsi probe_state selectors."""

    @pytest.mark.asyncio
    async def test_prejoin_fixture(self):
        provider = JitsiProvider()
        page = load_fixture("jitsi", "prejoin", title="Jitsi Meet", url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN

    @pytest.mark.asyncio
    async def test_lobby_fixture(self):
        provider = JitsiProvider()
        page = load_fixture("jitsi", "lobby", title="Jitsi Meet", url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.LOBBY

    @pytest.mark.asyncio
    async def test_in_meeting_fixture(self):
        provider = JitsiProvider()
        page = load_fixture("jitsi", "in_meeting", title="Jitsi Meet", url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING

    @pytest.mark.asyncio
    async def test_password_fixture(self):
        provider = JitsiProvider()
        page = load_fixture("jitsi", "password", title="Jitsi Meet", url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ERROR
        assert snapshot.error_code == "PASSWORD_REQUIRED"

    @pytest.mark.asyncio
    async def test_ended_fixture(self):
        provider = JitsiProvider()
        page = load_fixture("jitsi", "ended", title="Jitsi Meet", url="https://meet.jit.si/test-room")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ENDED


class TestWebexProviderFixtures:
    """Regression tests for Webex probe_state selectors."""

    @pytest.mark.asyncio
    async def test_prejoin_fixture(self, monkeypatch):
        provider = WebexProvider()
        page = load_fixture("webex", "prejoin", "page.html", title="Guest Join", url="https://company.webex.com/test")
        iframe = load_fixture("webex", "prejoin", "iframe.html")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN

    @pytest.mark.asyncio
    async def test_lobby_fixture(self, monkeypatch):
        provider = WebexProvider()
        page = load_fixture("webex", "lobby", "page.html", title="In Lobby", url="https://company.webex.com/test")
        iframe = load_fixture("webex", "lobby", "iframe.html")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.LOBBY

    @pytest.mark.asyncio
    async def test_in_meeting_fixture(self, monkeypatch):
        provider = WebexProvider()
        page = load_fixture(
            "webex",
            "in_meeting",
            "page.html",
            title="In Meeting",
            url="https://company.webex.com/test",
        )
        iframe = load_fixture("webex", "in_meeting", "iframe.html")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING

    @pytest.mark.asyncio
    async def test_password_fixture(self, monkeypatch):
        provider = WebexProvider()
        page = load_fixture(
            "webex", "password", "page.html", title="Enter password", url="https://company.webex.com/test"
        )
        iframe = load_fixture("webex", "password", "iframe.html")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ERROR
        assert snapshot.error_code == "PASSWORD_REQUIRED"

    @pytest.mark.asyncio
    async def test_ended_fixture(self, monkeypatch):
        provider = WebexProvider()
        page = load_fixture("webex", "ended", "page.html", title="Meeting", url="https://company.webex.com/test")
        iframe = load_fixture("webex", "ended", "iframe.html")
        monkeypatch.setattr(provider, "_get_webex_iframe", lambda _page: iframe)

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ENDED


class TestZoomProviderFixtures:
    """Regression tests for Zoom probe_state selectors."""

    @pytest.mark.asyncio
    async def test_prejoin_fixture(self):
        provider = ZoomProvider()
        page = load_fixture("zoom", "prejoin", title="Zoom Meeting", url="https://zoom.us/wc/join/123")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN

    @pytest.mark.asyncio
    async def test_launch_browser_join_fixture(self):
        provider = ZoomProvider()
        page = load_fixture(
            "zoom",
            "launch_browser_join",
            title="Zoom Workplace join page",
            url="https://zoom.us/w/123?tk=redacted&pwd=redacted&uuid=redacted#success",
        )

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.PREJOIN
        assert snapshot.evidence["zoom_stage"] == "cookie_blocked"
        assert snapshot.evidence["url"] == "https://zoom.us/w/123"

    @pytest.mark.asyncio
    async def test_lobby_fixture(self):
        provider = ZoomProvider()
        page = load_fixture("zoom", "lobby", title="Zoom Meeting", url="https://zoom.us/wc/join/123")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.LOBBY

    @pytest.mark.asyncio
    async def test_in_meeting_fixture(self):
        provider = ZoomProvider()
        page = load_fixture("zoom", "in_meeting", title="Zoom Meeting", url="https://zoom.us/wc/123/start")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.IN_MEETING

    @pytest.mark.asyncio
    async def test_in_meeting_hardware_warning_fixture_can_be_dismissed(self):
        provider = ZoomProvider()
        page = load_fixture(
            "zoom",
            "in_meeting_hardware_warning",
            title="Zoom Meeting",
            url="https://app.zoom.us/wc/123/join",
        )

        dismissed = await provider.dismiss_transient_overlays(page)

        assert dismissed is True
        assert 'button[aria-label*="close" i]' in page.clicked_selectors

    @pytest.mark.asyncio
    async def test_password_fixture(self):
        provider = ZoomProvider()
        page = load_fixture("zoom", "password", title="Zoom Meeting", url="https://zoom.us/wc/join/123")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ERROR
        assert snapshot.error_code == "PASSWORD_REQUIRED"

    @pytest.mark.asyncio
    async def test_ended_fixture(self):
        provider = ZoomProvider()
        page = load_fixture("zoom", "ended", title="Zoom Meeting", url="https://zoom.us/wc/123/leave")

        snapshot = await provider.probe_state(page)

        assert snapshot.state == MeetingState.ENDED
