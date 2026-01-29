"""Zoom Meeting Provider for Guest Join."""

import asyncio
import logging
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.async_api import Page

from providers.base import BaseProvider, JoinResult

logger = logging.getLogger(__name__)


class ZoomProvider(BaseProvider):
    """Provider for Zoom Meetings (Guest Join via Browser).

    Zoom requires special handling to bypass the desktop app download prompt
    and join via web browser. This is achieved by adding `?zc=0` parameter.
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
            Full meeting URL for guest join with zc=0 to skip client download
        """
        # If it's already a full URL, parse and add zc=0
        if meeting_code.startswith("http://") or meeting_code.startswith("https://"):
            parsed = urlparse(meeting_code)
            query_params = parse_qs(parsed.query)

            # Add zc=0 to skip Zoom client download
            query_params["zc"] = ["0"]

            # Rebuild URL with updated query
            new_query = urlencode(query_params, doseq=True)
            new_url = parsed._replace(query=new_query).geturl()
            return new_url

        # Build URL from meeting code
        base = base_url or "https://zoom.us"
        if not base.endswith("/"):
            base += "/"

        # Meeting ID format: typically 9-11 digits
        if meeting_code.replace(" ", "").replace("-", "").isdigit():
            # Numeric meeting ID
            clean_id = meeting_code.replace(" ", "").replace("-", "")
            return urljoin(base, f"j/{clean_id}?zc=0")
        else:
            # Personal meeting link
            return urljoin(base, f"j/{meeting_code}?zc=0")

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

        # Handle cookie consent if present
        try:
            cookie_btn = page.locator('button:has-text("Accept"), button:has-text("接受")')
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
                logger.info("Accepted cookie consent")
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # Look for "Join from Your Browser" link/button
        # Zoom often shows this as a fallback option
        browser_join_selectors = [
            'a:has-text("Join from Your Browser")',
            'a:has-text("從您的瀏覽器加入")',
            'a:has-text("join from your browser")',
            '[data-testid="join-from-browser-link"]',
            'a.joinWindowBtn:has-text("browser")',
            # Fallback: look for any link mentioning browser
            'a[href*="wc/join"]',
        ]

        browser_join_clicked = False
        for selector in browser_join_selectors:
            try:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info(f"Clicked 'Join from Browser' using: {selector}")
                    browser_join_clicked = True
                    await asyncio.sleep(3)
                    break
            except Exception:
                continue

        if not browser_join_clicked:
            logger.info("No 'Join from Browser' button found, assuming already on web client")

        # Wait for the web client to load
        await asyncio.sleep(2)

        # Fill in display name
        name_selectors = [
            "#inputname",
            'input[name="inputName"]',
            'input[placeholder*="name" i]',
            'input[placeholder*="名稱" i]',
            'input[aria-label*="name" i]',
            '[data-testid="input-name"]',
        ]

        name_filled = False
        for selector in name_selectors:
            try:
                name_input = page.locator(selector)
                if await name_input.count() > 0:
                    await name_input.first.click()
                    await name_input.first.fill(display_name)
                    logger.info(f"Display name filled using: {selector}")
                    name_filled = True
                    break
            except Exception:
                continue

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
                try:
                    password_input = page.locator(selector)
                    if await password_input.count() > 0:
                        await password_input.fill(password)
                        logger.info(f"Password filled using: {selector}")
                        break
                except Exception:
                    continue

        # Disable video/audio if possible (before joining)
        # Look for mute toggles on prejoin page
        await self._disable_media(page)

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
            try:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info(f"Disabled video using: {selector}")
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue

        # Audio off toggle
        audio_off_selectors = [
            'button[aria-label*="mute" i]:not([aria-label*="unmute" i])',
            'button[aria-label*="Mute My Audio" i]',
            'button[aria-label*="靜音" i]:not([aria-label*="取消" i])',
            "#preview-audio-btn",
        ]

        for selector in audio_off_selectors:
            try:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info(f"Disabled audio using: {selector}")
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue

    async def click_join(self, page: Page) -> None:
        """Click the join meeting button.

        Args:
            page: Playwright page instance
        """
        logger.info("Clicking Zoom join button")

        join_selectors = [
            'button:has-text("Join")',
            'button:has-text("加入")',
            'button:has-text("Join Meeting")',
            'button:has-text("加入會議")',
            "#joinBtn",
            '[data-testid="join-btn"]',
            "button.join-btn",
            'button[type="submit"]',
        ]

        for selector in join_selectors:
            try:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info(f"Clicked join button using: {selector}")
                    return
            except Exception:
                continue

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
            try:
                password_input = page.locator(selector)
                if await password_input.count() > 0:
                    await password_input.fill(password)
                    logger.info("Password filled in dialog")

                    # Find and click submit button
                    submit_selectors = [
                        'button:has-text("Join")',
                        'button:has-text("加入")',
                        'button:has-text("Submit")',
                        'button:has-text("OK")',
                        'button[type="submit"]',
                    ]

                    for btn_selector in submit_selectors:
                        btn = page.locator(btn_selector)
                        if await btn.count() > 0:
                            await btn.first.click()
                            logger.info("Password submitted")
                            return True

                    # Try pressing Enter
                    await password_input.press("Enter")
                    return True
            except Exception:
                continue

        return False

    async def wait_until_joined(self, page: Page, timeout_sec: int = 60, password: str | None = None) -> JoinResult:
        """Wait until successfully joined the Zoom meeting.

        Args:
            page: Playwright page instance
            timeout_sec: Maximum time to wait
            password: Optional password to apply if prompted

        Returns:
            JoinResult with success status
        """
        logger.info(f"Waiting to join Zoom meeting (timeout={timeout_sec}s)")

        start_time = asyncio.get_event_loop().time()
        check_interval = 2

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_sec:
                return JoinResult(
                    success=False,
                    error_code="timeout",
                    error_message=f"Timeout after {timeout_sec}s waiting to join",
                )

            # Check for password prompt
            if password:
                password_prompt = page.locator('input[type="password"], input[placeholder*="passcode" i]')
                if await password_prompt.count() > 0:
                    logger.info("Password prompt detected")
                    if await self.apply_password(page, password):
                        await asyncio.sleep(2)
                        continue

            # Check for waiting room / lobby
            lobby_indicators = [
                'text="Please wait"',
                'text="等待"',
                'text="Waiting Room"',
                'text="等候室"',
                'text="host will let you in"',
                'text="主持人將會讓您加入"',
                '[data-testid="waiting-room"]',
            ]

            for selector in lobby_indicators:
                try:
                    if await page.locator(selector).count() > 0:
                        logger.info("In Zoom waiting room")
                        return JoinResult(success=False, in_lobby=True)
                except Exception:
                    continue

            # Check for success - in meeting indicators
            in_meeting_indicators = [
                "#wc-footer",  # Web client footer
                '[data-testid="meeting-controls"]',
                'button[aria-label*="Mute" i]',
                'button[aria-label*="Stop Video" i]',
                "#wc-container-left",  # Participant panel
                ".meeting-app",
                '[data-testid="participants-btn"]',
            ]

            for selector in in_meeting_indicators:
                try:
                    if await page.locator(selector).count() > 0:
                        logger.info(f"Successfully joined meeting (detected: {selector})")
                        return JoinResult(success=True)
                except Exception:
                    continue

            # Check for errors
            error_indicators = [
                'text="Meeting has ended"',
                'text="會議已結束"',
                'text="Invalid meeting ID"',
                'text="無效的會議 ID"',
                'text="This meeting has been locked"',
                'text="此會議已鎖定"',
                'text="Meeting not started"',
                'text="會議尚未開始"',
            ]

            for selector in error_indicators:
                try:
                    element = page.locator(selector)
                    if await element.count() > 0:
                        error_text = await element.first.text_content()
                        logger.error(f"Meeting error: {error_text}")
                        return JoinResult(
                            success=False,
                            error_code="meeting_error",
                            error_message=error_text,
                        )
                except Exception:
                    continue

            await asyncio.sleep(check_interval)

    async def wait_in_lobby(self, page: Page, max_wait_sec: int = 900) -> bool:
        """Wait in Zoom waiting room until admitted.

        Args:
            page: Playwright page instance
            max_wait_sec: Maximum wait time (default 15 minutes)

        Returns:
            True if admitted, False if timeout
        """
        logger.info(f"Waiting in Zoom lobby (max wait: {max_wait_sec}s)")

        start_time = asyncio.get_event_loop().time()
        check_interval = 5

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > max_wait_sec:
                logger.warning("Lobby timeout")
                return False

            # Check if we're now in the meeting
            in_meeting_indicators = [
                "#wc-footer",
                'button[aria-label*="Mute" i]',
                'button[aria-label*="Stop Video" i]',
                '[data-testid="meeting-controls"]',
            ]

            for selector in in_meeting_indicators:
                try:
                    if await page.locator(selector).count() > 0:
                        logger.info("Admitted from waiting room")
                        return True
                except Exception:
                    continue

            # Check if meeting ended while waiting
            if await self.detect_meeting_end(page):
                logger.warning("Meeting ended while in waiting room")
                return False

            await asyncio.sleep(check_interval)

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

    async def detect_meeting_end(self, page: Page) -> bool:
        """Check if Zoom meeting has ended.

        Args:
            page: Playwright page instance

        Returns:
            True if meeting ended
        """
        end_indicators = [
            'text="Meeting has ended"',
            'text="會議已結束"',
            'text="The host has ended this meeting"',
            'text="主持人已結束此會議"',
            'text="You have been removed"',
            'text="您已被移除"',
            'text="Meeting ended"',
            '[data-testid="meeting-ended"]',
        ]

        for selector in end_indicators:
            try:
                if await page.locator(selector).count() > 0:
                    logger.info(f"Meeting end detected: {selector}")
                    return True
            except Exception:
                continue

        # Check page title for end indicators
        try:
            title = await page.title()
            title_lower = title.lower()
            if "ended" in title_lower or "結束" in title:
                logger.info(f"Meeting end detected from title: {title}")
                return True
        except Exception:
            pass

        return False
