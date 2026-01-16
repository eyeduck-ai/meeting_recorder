import asyncio
import logging
from urllib.parse import urljoin

from playwright.async_api import Page

from config.settings import get_settings
from providers.base import BaseProvider, JoinResult

logger = logging.getLogger(__name__)


class JitsiProvider(BaseProvider):
    """Provider for Jitsi Meet (meet.jit.si)."""

    @property
    def name(self) -> str:
        return "jitsi"

    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        """Build Jitsi meeting URL.

        Args:
            meeting_code: Room name (e.g., 'my-meeting-room')
            base_url: Base URL (default: https://meet.jit.si/)

        Returns:
            Full meeting URL with config to start muted
        """
        if base_url is None:
            base_url = get_settings().jitsi_base_url

        # Ensure base_url ends with /
        if not base_url.endswith("/"):
            base_url += "/"

        # Add config parameters to start with video and audio muted
        config_params = "#config.startWithVideoMuted=true&config.startWithAudioMuted=true"
        return urljoin(base_url, meeting_code) + config_params

    async def prejoin(
        self,
        page: Page,
        display_name: str,
        password: str | None = None,
    ) -> None:
        """Handle Jitsi prejoin page.

        Args:
            page: Playwright page instance
            display_name: Name to display in meeting
            password: Room password if required
        """
        logger.info(f"Handling prejoin page, display_name={display_name}")

        # Video and audio are already muted via URL config params
        # Wait for join button to appear (indicates page is fully loaded)
        join_button = page.locator('[data-testid="prejoin.joinMeeting"]')
        await join_button.wait_for(state="visible", timeout=15000)

        # Find and fill display name - try multiple selectors
        name_selectors = [
            'input[data-testid="prejoin.input"]',
            'input[placeholder*="name" i]',
            'input[placeholder*="Enter your name" i]',
            'input[placeholder*="輸入你的名稱" i]',
        ]

        for selector in name_selectors:
            name_input = page.locator(selector)
            if await name_input.count() > 0:
                await name_input.first.click()
                await name_input.first.fill(display_name)
                logger.info(f"Display name filled using: {selector}")
                break
        else:
            logger.warning("Could not find display name input")

        # Handle "unsafe room name" consent checkbox if present
        consent_checkbox = page.locator('label[class*="consent"] input[type="checkbox"]')
        if await consent_checkbox.count() > 0:
            is_checked = await consent_checkbox.is_checked()
            if not is_checked:
                # Use force=True because SVG checkmark icon intercepts pointer events
                await consent_checkbox.click(force=True)
                logger.info("Consent checkbox clicked (unsafe room name warning)")
                await asyncio.sleep(0.5)

        # Handle password if provided
        if password:
            password_input = page.locator('input[type="password"]')
            if await password_input.count() > 0:
                await password_input.fill(password)
                logger.info("Password filled")

    async def click_join(self, page: Page) -> None:
        """Click the join meeting button.

        Args:
            page: Playwright page instance
        """
        logger.info("Clicking join button")

        # Try primary join button
        join_button = page.locator('[data-testid="prejoin.joinMeeting"]')
        if await join_button.count() > 0:
            await join_button.click()
            return

        # Try alternative selectors
        alt_selectors = [
            'button:has-text("Join meeting")',
            'button:has-text("Join")',
            'button:has-text("加入會議")',
            'button:has-text("加入")',
        ]

        for selector in alt_selectors:
            button = page.locator(selector)
            if await button.count() > 0:
                await button.first.click()
                logger.info(f"Clicked join button with selector: {selector}")
                return

        raise RuntimeError("Could not find join button")

    async def apply_password(self, page: Page, password: str) -> bool:
        """Apply password when prompted after joining.

        Args:
            page: Playwright page instance
            password: Room password

        Returns:
            True if password was entered successfully
        """
        logger.info("Checking for password dialog")

        # Wait a moment for dialog to appear
        await asyncio.sleep(1)

        # Look for password dialog
        password_selectors = [
            'input[name="lockKey"]',
            'input[type="password"]',
            'input[placeholder*="password" i]',
            'input[placeholder*="密碼" i]',
        ]

        for selector in password_selectors:
            password_input = page.locator(selector)
            if await password_input.count() > 0:
                await password_input.fill(password)
                logger.info("Password filled in dialog")

                # Find and click OK/Submit button
                submit_selectors = [
                    'button:has-text("OK")',
                    'button:has-text("Submit")',
                    'button:has-text("確定")',
                    'button:has-text("Enter")',
                    'button[type="submit"]',
                ]

                for btn_selector in submit_selectors:
                    btn = page.locator(btn_selector)
                    if await btn.count() > 0:
                        await btn.first.click()
                        logger.info("Password submitted")
                        return True

                # Try pressing Enter if no button found
                await password_input.press("Enter")
                return True

        return False

    async def wait_until_joined(self, page: Page, timeout_sec: int = 60, password: str | None = None) -> JoinResult:
        """Wait until successfully joined the meeting.

        Uses priority-based state detection: detects ALL possible states simultaneously,
        then returns based on priority (in_meeting > error > lobby).
        This prevents blocking on lobby detection when meetings go directly to joined state.

        Args:
            page: Playwright page instance
            timeout_sec: Maximum time to wait
            password: Optional password to apply if prompted

        Returns:
            JoinResult with success status
        """
        logger.info(f"Waiting to join meeting (timeout={timeout_sec}s)")

        start_time = asyncio.get_event_loop().time()
        end_time = start_time + timeout_sec
        password_attempted = False

        while asyncio.get_event_loop().time() < end_time:
            # ============================================================
            # PHASE 1: Detect ALL possible states simultaneously
            # ============================================================

            # Check in-meeting indicators (highest priority)
            # These elements ONLY appear when successfully joined:
            # - #filmstripLocalVideo: local video filmstrip (most reliable)
            # - #remoteVideos: remote video filmstrip
            # - .details-container: meeting details bar (timer, subject name)
            in_meeting_indicators = page.locator("#filmstripLocalVideo," "#remoteVideos," ".details-container")
            is_in_meeting = await in_meeting_indicators.count() > 0

            # Check error indicators
            error_result = None
            error_indicators = [
                ('text="Meeting not found"', "MEETING_NOT_FOUND", "會議不存在"),
                ('text="會議不存在"', "MEETING_NOT_FOUND", "會議不存在"),
                ('text="Password required"', "PASSWORD_REQUIRED", "需要密碼"),
                ('text="需要密碼"', "PASSWORD_REQUIRED", "需要密碼"),
                ('text="Wrong password"', "PASSWORD_INCORRECT", "密碼錯誤"),
                ('text="密碼錯誤"', "PASSWORD_INCORRECT", "密碼錯誤"),
                ('text="Invalid password"', "PASSWORD_INCORRECT", "密碼錯誤"),
            ]
            for indicator, error_code, error_msg in error_indicators:
                if await page.locator(indicator).count() > 0:
                    error_result = (error_code, error_msg)
                    break

            # Check lobby indicators (lowest priority - may not exist in Jitsi)
            # .lobby-screen is the primary indicator
            is_in_lobby = False
            lobby_screen = page.locator(".lobby-screen")
            if await lobby_screen.count() > 0:
                is_in_lobby = True
            else:
                # Fallback lobby checks
                lobby_indicators = [
                    'text="Waiting for the host"',
                    'text="等待主持人"',
                    'text="You are in the waiting room"',
                    'text="Asking to join meeting"',
                ]
                for indicator in lobby_indicators:
                    if await page.locator(indicator).count() > 0:
                        is_in_lobby = True
                        break

            # ============================================================
            # PHASE 2: Return based on priority (in_meeting > error > lobby)
            # ============================================================

            # Priority 1: In meeting (highest - immediately return success)
            if is_in_meeting:
                logger.info("Successfully joined meeting")
                return JoinResult(success=True, in_lobby=False)

            # Priority 2: Error (return error, but password dialog may be handled)
            if error_result:
                error_code, error_msg = error_result
                # Special case: handle password dialog if password is provided
                if error_code == "PASSWORD_REQUIRED" and password and not password_attempted:
                    password_dialog = page.locator('input[name="lockKey"], input[type="password"]:visible')
                    if await password_dialog.count() > 0:
                        if await self.apply_password(page, password):
                            password_attempted = True
                            await asyncio.sleep(2)
                            continue
                logger.error(f"Join error: {error_msg}")
                return JoinResult(
                    success=False,
                    in_lobby=False,
                    error_code=error_code,
                    error_message=error_msg,
                )

            # Priority 3: In lobby (return lobby status)
            if is_in_lobby:
                logger.info("Detected lobby: waiting for moderator")
                return JoinResult(success=False, in_lobby=True)

            # None of the states detected yet, continue waiting
            await asyncio.sleep(1)

        logger.error("Timeout waiting to join meeting")
        return JoinResult(
            success=False,
            in_lobby=False,
            error_code="JOIN_TIMEOUT",
            error_message=f"Timeout after {timeout_sec} seconds",
        )

    async def wait_in_lobby(self, page: Page, max_wait_sec: int = 900) -> bool:
        """Wait in lobby until admitted.

        Args:
            page: Playwright page instance
            max_wait_sec: Maximum wait time (default 15 minutes)

        Returns:
            True if admitted, False if timeout
        """
        logger.info(f"Waiting in lobby (max={max_wait_sec}s)")

        start_time = asyncio.get_event_loop().time()
        end_time = start_time + max_wait_sec
        check_interval = 5  # Check every 5 seconds

        while asyncio.get_event_loop().time() < end_time:
            # Check if now in meeting
            in_meeting = page.locator("#largeVideoContainer, .videocontainer")
            if await in_meeting.count() > 0:
                logger.info("Admitted from lobby")
                return True

            # Check if rejected
            rejected_indicators = [
                'text="rejected"',
                'text="denied"',
                'text="拒絕"',
            ]
            for indicator in rejected_indicators:
                if await page.locator(indicator).count() > 0:
                    logger.error("Rejected from lobby")
                    return False

            elapsed = int(asyncio.get_event_loop().time() - start_time)
            if elapsed % 60 == 0:  # Log every minute
                logger.info(f"Still waiting in lobby... ({elapsed}s elapsed)")

            await asyncio.sleep(check_interval)

        logger.error(f"Lobby timeout after {max_wait_sec}s")
        return False

    async def set_layout(self, page: Page, preset: str = "speaker") -> bool:
        """Attempt to set speaker view layout.

        Args:
            page: Playwright page instance
            preset: Layout preset (only 'speaker' supported currently)

        Returns:
            True if layout was set
        """
        if preset != "speaker":
            logger.warning(f"Unsupported layout preset: {preset}, using speaker")

        logger.info("Attempting to set speaker view layout")

        try:
            # Try to click tile view button to ensure we're not in tile view
            tile_button = page.locator('[aria-label*="tile" i], [aria-label*="grid" i]')
            if await tile_button.count() > 0:
                # Check current state and toggle if needed
                await tile_button.first.click()
                await asyncio.sleep(0.5)
                # Check if we need to click again to get to speaker view
                await tile_button.first.click()
                logger.info("Layout toggled")

            return True
        except Exception as e:
            logger.warning(f"Could not set layout: {e}")
            return False

    async def detect_meeting_end(self, page: Page) -> bool:
        """Check if meeting has ended.

        Args:
            page: Playwright page instance

        Returns:
            True if meeting ended
        """
        try:
            # Check text indicators for meeting end
            end_indicators = [
                'text="meeting has ended"',
                'text="會議已結束"',
                'text="You have been disconnected"',
                'text="連線已中斷"',
                'text="kicked"',
                'text="Conference not found"',
                'text="會議不存在"',
            ]

            for indicator in end_indicators:
                if await page.locator(indicator).count() > 0:
                    logger.info("Meeting end detected: text indicator found")
                    return True

            # Check if page navigated away from meeting
            url = page.url
            if "meet.jit.si" in url and "/" not in url.split("meet.jit.si")[1].strip("/"):
                logger.info("Navigated away from meeting")
                return True

            # Check if video elements are gone (meeting may have ended)
            video_count = await page.locator("video").count()
            if video_count == 0:
                # Double-check: wait a moment and check again to avoid false positives
                await asyncio.sleep(2)
                video_count = await page.locator("video").count()
                if video_count == 0:
                    logger.info("No video elements found - meeting may have ended")
                    return True

            # Check for "You are alone" indicator (host left)
            alone_indicators = [
                'text="You are the only one in the meeting"',
                'text="你是會議中唯一的人"',
            ]
            for indicator in alone_indicators:
                if await page.locator(indicator).count() > 0:
                    logger.debug("Only participant remaining in meeting")
                    # Don't end yet - host might return

        except Exception as e:
            logger.debug(f"Error in detect_meeting_end: {e}")

        return False
