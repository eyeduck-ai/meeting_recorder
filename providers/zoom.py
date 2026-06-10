"""Zoom Meeting Provider for Guest Join."""

import asyncio
import logging
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.async_api import Page

from providers.base import BaseProvider, MeetingState, MeetingStateSnapshot

logger = logging.getLogger(__name__)


class ZoomProvider(BaseProvider):
    """Provider for Zoom Meetings (Guest Join via Browser).

    Zoom requires special handling to bypass the desktop app download prompt
    and join via web browser. We use the `/wc/join/{meeting_id}` web client
    path when possible and keep `zc=0` as a fallback hint.
    """

    @property
    def name(self) -> str:
        return "zoom"

    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        """Build Zoom guest join URL.

        Args:
            meeting_code: Meeting ID or full meeting URL
            base_url: Base URL (e.g., https://zoom.us)

        Returns:
            Full meeting URL for guest join through the Zoom web client.
        """
        web_client_params = {"fromPWA": ["1"], "zc": ["0"]}

        # If it's already a full URL, convert Zoom launch URLs to the web
        # client path and preserve passcode/query parameters.
        if meeting_code.startswith("http://") or meeting_code.startswith("https://"):
            parsed = urlparse(meeting_code)
            query_params = parse_qs(parsed.query)
            query_params.update(web_client_params)

            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] == "j":
                parsed = parsed._replace(path=f"/wc/join/{path_parts[1]}")
            elif len(path_parts) >= 3 and path_parts[0] == "wc" and path_parts[1] == "join":
                parsed = parsed._replace(path=f"/wc/join/{path_parts[2]}")

            new_query = urlencode(query_params, doseq=True)
            return parsed._replace(query=new_query).geturl()

        # Build URL from meeting code
        base = base_url or "https://zoom.us"
        if not base.endswith("/"):
            base += "/"

        # Meeting ID format: typically 9-11 digits
        if meeting_code.replace(" ", "").replace("-", "").isdigit():
            # Numeric meeting ID
            clean_id = meeting_code.replace(" ", "").replace("-", "")
            return urljoin(base, f"wc/join/{clean_id}?fromPWA=1&zc=0")
        else:
            # Personal meeting link
            return urljoin(base, f"j/{meeting_code}?fromPWA=1&zc=0")

    async def prejoin(
        self,
        page: Page,
        display_name: str,
        password: str | None = None,
    ) -> None:
        """Handle Zoom guest join page.

        Args:
            page: Playwright page instance
            display_name: Name to display in meeting
            password: Meeting password if required
        """
        logger.info(f"Handling Zoom prejoin page, display_name={display_name}")

        # Wait for page to load
        await asyncio.sleep(3)

        # Handle cookie consent if present. Zoom's cookie prompt can cover
        # the browser-join button and prevent the click from reaching it.
        await self._accept_cookie_consent(page)

        name_selectors = [
            "#input-for-name",
            "#inputname",
            'input[name="inputName"]',
            'input[placeholder*="name" i]',
            'input[placeholder*="名稱" i]',
            'input[aria-label*="name" i]',
            '[data-testid="input-name"]',
            'input[type="text"]',
        ]

        if not await self._has_any_selector(page, name_selectors):
            await self._click_browser_join(page)

        await asyncio.sleep(2)

        if not await self._has_any_selector(page, name_selectors) and "/wc/" not in page.url:
            web_client_url = self.build_join_url(page.url)
            if web_client_url != page.url:
                logger.info(f"Navigating directly to Zoom web client: {web_client_url}")
                await page.goto(web_client_url, wait_until="domcontentloaded")
                await asyncio.sleep(5)

        # Fill in display name
        name_filled = await self._fill_display_name(page, name_selectors, display_name)

        if not name_filled:
            logger.warning("Could not find display name input")

        # Handle password if provided and password field is visible
        if password:
            password_selectors = [
                "#inputpasscode",
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="password" i]',
                'input[placeholder*="密碼" i]',
                'input[placeholder*="passcode" i]',
            ]

            for selector in password_selectors:
                for target in self._targets(page):
                    try:
                        password_input = target.locator(selector)
                        if await password_input.count() > 0:
                            await password_input.first.fill(password)
                            logger.info(f"Password filled using: {selector}")
                            break
                    except Exception:
                        continue

        # Disable video/audio if possible (before joining)
        # Look for mute toggles on prejoin page
        await self._disable_media(page)

    async def _accept_cookie_consent(self, page: Page) -> None:
        """Accept Zoom/OneTrust cookie consent if it is blocking the page."""
        cookie_selectors = [
            "#onetrust-accept-btn-handler",
            'button:has-text("Accept Cookies")',
            'button:has-text("ACCEPT COOKIES")',
            'button:has-text("Accept")',
            'button:has-text("接受")',
            'button[aria-label*="Accept" i]',
            '[id*="onetrust-accept"]',
        ]
        for selector in cookie_selectors:
            try:
                cookie_btn = page.locator(selector)
                if await cookie_btn.count() > 0:
                    try:
                        await cookie_btn.first.click(timeout=3000)
                    except Exception:
                        await cookie_btn.first.click(timeout=3000, force=True)
                    logger.info(f"Accepted cookie consent using: {selector}")
                    await self._wait_until_hidden(page, ["#onetrust-banner-sdk", ".onetrust-pc-dark-filter"])
                    break
            except Exception:
                continue

    async def _click_browser_join(self, page: Page) -> bool:
        """Click Zoom's browser-join fallback on launch pages."""
        # Look for "Join from Your Browser" link/button
        # Zoom often shows this as a fallback option
        browser_join_selectors = [
            'button:has-text("Join from browser")',
            'button:has-text("Join from Browser")',
            '[role="button"]:has-text("Join from browser")',
            'a:has-text("Join from Your Browser")',
            'a:has-text("從您的瀏覽器加入")',
            'a:has-text("join from your browser")',
            '[data-testid="join-from-browser-link"]',
            'a.joinWindowBtn:has-text("browser")',
            'button:has-text("瀏覽器")',
            # Fallback: look for any link mentioning browser
            'a[href*="wc/join"]',
        ]

        for selector in browser_join_selectors:
            try:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    try:
                        await btn.first.click(timeout=3000)
                    except Exception:
                        await btn.first.click(timeout=3000, force=True)
                    logger.info(f"Clicked 'Join from Browser' using: {selector}")
                    await asyncio.sleep(5)
                    return True
            except Exception:
                continue

        logger.info("No 'Join from Browser' button found, assuming already on web client")
        return False

    async def _has_any_selector(self, page: Page, selectors: list[str]) -> bool:
        """Return True when any selector is present on the page."""
        for target in self._targets(page):
            for selector in selectors:
                try:
                    if await target.locator(selector).count() > 0:
                        return True
                except Exception:
                    continue
        return False

    def _targets(self, page: Page) -> list:
        """Return the page plus any child frames that may contain Zoom's PWA UI."""
        targets = [page]
        for frame in getattr(page, "frames", []) or []:
            if frame not in targets:
                targets.append(frame)
        return targets

    async def _click_first(self, page: Page, selectors: list[str], description: str) -> bool:
        """Click the first visible element matching any selector in page or frame."""
        for target in self._targets(page):
            for selector in selectors:
                try:
                    locator = target.locator(selector)
                    count = await locator.count()
                    for index in range(count):
                        element = locator.nth(index)
                        if not await element.is_visible():
                            continue
                        try:
                            await element.click(timeout=3000)
                        except Exception:
                            await element.click(timeout=3000, force=True)
                        logger.info(f"Clicked {description} using: {selector}")
                        return True
                except Exception:
                    continue
        return False

    async def _fill_display_name(self, page: Page, selectors: list[str], display_name: str) -> bool:
        """Wait for and fill the Zoom display-name field."""
        for _ in range(20):
            for target in self._targets(page):
                for selector in selectors:
                    try:
                        name_input = target.locator(selector)
                        count = await name_input.count()
                        for index in range(count):
                            element = name_input.nth(index)
                            if not await element.is_visible():
                                continue
                            await element.click(timeout=3000)
                            await element.fill(display_name, timeout=3000)
                            logger.info(f"Display name filled using: {selector}")
                            return True
                    except Exception:
                        continue
            await asyncio.sleep(1)
        return False

    async def _wait_until_hidden(self, page: Page, selectors: list[str]) -> None:
        """Best-effort wait for blocking overlays to disappear."""
        for selector in selectors:
            try:
                overlay = page.locator(selector)
                if await overlay.count() > 0:
                    await overlay.first.wait_for(state="hidden", timeout=3000)
            except Exception:
                continue

    async def _disable_media(self, page: Page) -> None:
        """Attempt to disable video and audio on prejoin page."""
        # Video off toggle
        video_off_selectors = [
            'button[aria-label*="video" i][aria-pressed="true"]',
            'button[aria-label*="Stop Video" i]',
            'button[aria-label*="停止視訊" i]',
            "#preview-video-btn",
        ]

        for selector in video_off_selectors:
            if await self._click_first(page, [selector], "video toggle"):
                await asyncio.sleep(0.3)
                break

        # Audio off toggle
        audio_off_selectors = [
            'button[aria-label*="mute" i]:not([aria-label*="unmute" i])',
            'button[aria-label*="Mute My Audio" i]',
            'button[aria-label*="靜音" i]:not([aria-label*="取消" i])',
            "#preview-audio-btn",
        ]

        for selector in audio_off_selectors:
            if await self._click_first(page, [selector], "audio toggle"):
                await asyncio.sleep(0.3)
                break

    async def click_join(self, page: Page) -> None:
        """Click the join meeting button.

        Args:
            page: Playwright page instance
        """
        logger.info("Clicking Zoom join button")

        join_selectors = [
            "#joinBtn",
            '[data-testid="join-btn"]',
            "button.join-btn",
            "button.preview-join-button",
            ".preview-join-button",
            'button:has-text("Join Meeting")',
            'button:has-text("加入會議")',
            'button[type="submit"]',
        ]
        if "/wc/" in page.url:
            join_selectors.extend(
                [
                    'button:has-text("Join")',
                    'button:has-text("加入")',
                ]
            )

        if await self._click_first(page, join_selectors, "join button"):
            return

        logger.warning("Could not find join button")

    async def apply_password(self, page: Page, password: str) -> bool:
        """Apply password when prompted.

        Args:
            page: Playwright page instance
            password: Meeting password

        Returns:
            True if password was entered successfully
        """
        logger.info("Checking for Zoom password dialog")

        await asyncio.sleep(1)

        password_selectors = [
            "#inputpasscode",
            'input[type="password"]',
            'input[placeholder*="password" i]',
            'input[placeholder*="passcode" i]',
            'input[placeholder*="密碼" i]',
        ]

        for selector in password_selectors:
            for target in self._targets(page):
                try:
                    password_input = target.locator(selector)
                    if await password_input.count() > 0:
                        await password_input.first.fill(password)
                        logger.info("Password filled in dialog")

                        # Find and click submit button
                        submit_selectors = [
                            'button:has-text("Join")',
                            'button:has-text("加入")',
                            'button:has-text("Submit")',
                            'button:has-text("OK")',
                            'button[type="submit"]',
                        ]

                        if await self._click_first(page, submit_selectors, "password submit button"):
                            return True

                        # Try pressing Enter
                        await password_input.first.press("Enter")
                        return True
                except Exception:
                    continue
        return False

    async def probe_state(self, page: Page) -> MeetingStateSnapshot:
        """Probe the current Zoom meeting state."""
        lobby_selectors = [
            'text="Please wait"',
            'text="等待"',
            'text="Waiting Room"',
            'text="等候室"',
            'text="host will let you in"',
            'text="主持人將會讓您加入"',
            '[data-testid="waiting-room"]',
        ]
        in_meeting_selectors = [
            "#wc-footer",
            '[data-testid="meeting-controls"]',
            "#wc-container-left",
            ".meeting-app",
            '[data-testid="participants-btn"]',
        ]
        ended_selectors = [
            'text="Meeting has ended"',
            'text="會議已結束"',
            'text="The host has ended this meeting"',
            'text="主持人已結束此會議"',
            'text="You have been removed"',
            'text="您已被移除"',
            '[data-testid="meeting-ended"]',
        ]
        error_selectors = [
            ('text="Invalid meeting ID"', "MEETING_NOT_FOUND", "無效的會議 ID"),
            ('text="無效的會議 ID"', "MEETING_NOT_FOUND", "無效的會議 ID"),
            ('text="This meeting has been locked"', "MEETING_LOCKED", "此會議已鎖定"),
            ('text="此會議已鎖定"', "MEETING_LOCKED", "此會議已鎖定"),
            ('text="Meeting not started"', "MEETING_NOT_STARTED", "會議尚未開始"),
            ('text="會議尚未開始"', "MEETING_NOT_STARTED", "會議尚未開始"),
        ]
        prejoin_selectors = [
            "#input-for-name",
            "#inputname",
            'input[name="inputName"]',
            "#joinBtn",
            '[data-testid="join-btn"]',
            "button.preview-join-button",
            'text="Enter Meeting Info"',
            'text="Your Name"',
            'button:has-text("Join from browser")',
            'button:has-text("Join from Zoom Workplace app")',
        ]
        password_selectors = [
            "#inputpasscode",
            'input[type="password"]',
            'input[placeholder*="passcode" i]',
        ]

        matched_in_meeting = []
        for selector in in_meeting_selectors:
            if await self._has_any_selector(page, [selector]):
                matched_in_meeting.append(selector)
        if matched_in_meeting:
            return MeetingStateSnapshot(
                state=MeetingState.IN_MEETING,
                reason="Detected Zoom in-meeting UI",
                evidence={"matched_selectors": matched_in_meeting, "url": page.url},
            )

        matched_ended = []
        for selector in ended_selectors:
            if await self._has_any_selector(page, [selector]):
                matched_ended.append(selector)
        if matched_ended:
            return MeetingStateSnapshot(
                state=MeetingState.ENDED,
                reason="Detected Zoom meeting ended UI",
                evidence={"matched_selectors": matched_ended, "url": page.url},
                error_code="MEETING_ENDED",
                error_message="Meeting ended before recording could continue",
            )

        for selector, error_code, error_message in error_selectors:
            if await self._has_any_selector(page, [selector]):
                return MeetingStateSnapshot(
                    state=MeetingState.ERROR,
                    reason=f"Detected Zoom error selector: {selector}",
                    evidence={"matched_selectors": [selector], "url": page.url},
                    error_code=error_code,
                    error_message=error_message,
                )

        matched_lobby = []
        for selector in lobby_selectors:
            if await self._has_any_selector(page, [selector]):
                matched_lobby.append(selector)
        if matched_lobby:
            return MeetingStateSnapshot(
                state=MeetingState.LOBBY,
                reason="Detected Zoom waiting room UI",
                evidence={"matched_selectors": matched_lobby, "url": page.url},
            )

        matched_password = []
        for selector in password_selectors:
            if await self._has_any_selector(page, [selector]):
                matched_password.append(selector)
        if matched_password:
            return MeetingStateSnapshot(
                state=MeetingState.ERROR,
                reason="Zoom password prompt detected",
                evidence={"matched_selectors": matched_password, "password_prompt": True, "url": page.url},
                error_code="PASSWORD_REQUIRED",
                error_message="需要密碼",
            )

        matched_prejoin = []
        for selector in prejoin_selectors:
            if await self._has_any_selector(page, [selector]):
                matched_prejoin.append(selector)
        if matched_prejoin:
            return MeetingStateSnapshot(
                state=MeetingState.PREJOIN,
                reason="Detected Zoom prejoin UI",
                evidence={"matched_selectors": matched_prejoin, "url": page.url},
            )

        return MeetingStateSnapshot(
            state=MeetingState.JOINING,
            reason="Waiting for Zoom to transition to the next state",
            confidence=0.5,
            evidence={"url": page.url},
        )

    async def set_layout(self, page: Page, preset: str = "speaker") -> bool:
        """Attempt to set Zoom video layout.

        Args:
            page: Playwright page instance
            preset: Layout preset (speaker or gallery)

        Returns:
            True if layout was set
        """
        logger.info(f"Attempting to set Zoom layout: {preset}")

        # Zoom web client layout controls
        try:
            # Look for view button
            view_btn = page.locator('button[aria-label*="View" i], button:has-text("View"), button:has-text("檢視")')
            if await view_btn.count() > 0:
                await view_btn.first.click()
                await asyncio.sleep(0.5)

                if preset == "speaker":
                    speaker_option = page.locator('text="Speaker View", text="演講者檢視", [aria-label*="Speaker" i]')
                else:
                    speaker_option = page.locator('text="Gallery View", text="畫廊檢視", [aria-label*="Gallery" i]')

                if await speaker_option.count() > 0:
                    await speaker_option.first.click()
                    logger.info(f"Layout set to {preset}")
                    return True
        except Exception as e:
            logger.debug(f"Could not set layout: {e}")

        return False
