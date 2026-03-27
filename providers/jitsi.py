import asyncio
import logging
from urllib.parse import urljoin

from playwright.async_api import Page

from config.settings import get_settings
from providers.base import BaseProvider, MeetingState, MeetingStateSnapshot

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

        # Add config parameters:
        # - Start with video and audio muted
        # - Disable tile view to use stage view (better for screen sharing)
        config_params = "#config.startWithVideoMuted=true&config.startWithAudioMuted=true&config.disableTileView=true"
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

    async def probe_state(self, page: Page) -> MeetingStateSnapshot:
        """Probe the current Jitsi meeting state."""
        in_meeting_selectors = [
            "#filmstripLocalVideo",
            "#remoteVideos",
            ".details-container",
        ]
        ended_indicators = [
            'text="meeting has ended"',
            'text="會議已結束"',
            'text="You have been disconnected"',
            'text="連線已中斷"',
            'text="Conference not found"',
            'text="會議不存在"',
        ]
        error_indicators = [
            ('text="Meeting not found"', "MEETING_NOT_FOUND", "會議不存在"),
            ('text="會議不存在"', "MEETING_NOT_FOUND", "會議不存在"),
            ('text="Password required"', "PASSWORD_REQUIRED", "需要密碼"),
            ('text="需要密碼"', "PASSWORD_REQUIRED", "需要密碼"),
            ('text="Wrong password"', "PASSWORD_INCORRECT", "密碼錯誤"),
            ('text="密碼錯誤"', "PASSWORD_INCORRECT", "密碼錯誤"),
            ('text="Invalid password"', "PASSWORD_INCORRECT", "密碼錯誤"),
        ]
        lobby_selectors = [
            ".lobby-screen",
            'text="Waiting for the host"',
            'text="等待主持人"',
            'text="You are in the waiting room"',
            'text="Asking to join meeting"',
        ]
        prejoin_selectors = [
            '[data-testid="prejoin.joinMeeting"]',
            'input[data-testid="prejoin.input"]',
        ]
        password_selectors = [
            'input[name="lockKey"]',
            'input[type="password"]',
        ]

        matched_in_meeting = []
        for selector in in_meeting_selectors:
            if await page.locator(selector).count() > 0:
                matched_in_meeting.append(selector)
        if matched_in_meeting:
            return MeetingStateSnapshot(
                state=MeetingState.IN_MEETING,
                reason="Detected in-meeting Jitsi UI",
                evidence={"matched_selectors": matched_in_meeting, "url": page.url},
            )

        matched_ended = []
        for selector in ended_indicators:
            if await page.locator(selector).count() > 0:
                matched_ended.append(selector)
        if matched_ended:
            return MeetingStateSnapshot(
                state=MeetingState.ENDED,
                reason="Detected meeting ended indicator",
                evidence={"matched_selectors": matched_ended, "url": page.url},
                error_code="MEETING_ENDED",
                error_message="Meeting ended before recording could continue",
            )

        for selector, error_code, error_message in error_indicators:
            if await page.locator(selector).count() > 0:
                return MeetingStateSnapshot(
                    state=MeetingState.ERROR,
                    reason=f"Detected error selector: {selector}",
                    evidence={
                        "matched_selectors": [selector],
                        "url": page.url,
                        "password_prompt": error_code.startswith("PASSWORD"),
                    },
                    error_code=error_code,
                    error_message=error_message,
                )

        matched_lobby = []
        for selector in lobby_selectors:
            if await page.locator(selector).count() > 0:
                matched_lobby.append(selector)
        if matched_lobby:
            return MeetingStateSnapshot(
                state=MeetingState.LOBBY,
                reason="Detected Jitsi lobby UI",
                evidence={"matched_selectors": matched_lobby, "url": page.url},
            )

        matched_prejoin = []
        for selector in prejoin_selectors:
            if await page.locator(selector).count() > 0:
                matched_prejoin.append(selector)
        if matched_prejoin:
            return MeetingStateSnapshot(
                state=MeetingState.PREJOIN,
                reason="Detected Jitsi prejoin UI",
                evidence={"matched_selectors": matched_prejoin, "url": page.url},
            )

        password_prompt = []
        for selector in password_selectors:
            if await page.locator(selector).count() > 0:
                password_prompt.append(selector)
        if password_prompt:
            return MeetingStateSnapshot(
                state=MeetingState.ERROR,
                reason="Password dialog detected",
                evidence={"matched_selectors": password_prompt, "password_prompt": True, "url": page.url},
                error_code="PASSWORD_REQUIRED",
                error_message="需要密碼",
            )

        return MeetingStateSnapshot(
            state=MeetingState.JOINING,
            reason="Waiting for Jitsi to transition to the next state",
            confidence=0.5,
            evidence={"url": page.url},
        )

    async def set_layout(self, page: Page, preset: str = "speaker") -> bool:
        """Attempt to set speaker view layout.

        Strategy:
        1. Check if we are in Tile View (look for .tile-view class).
        2. If in Tile View, press 'w' to toggle.
        3. Double check and retry with button if needed.

        Args:
            page: Playwright page instance
            preset: Layout preset (only 'speaker' supported currently)

        Returns:
            True if layout seems correct (Speaker View)
        """
        if preset != "speaker":
            logger.warning(f"Unsupported layout preset: {preset}, using speaker")

        logger.info("Attempting to ensure speaker view layout")

        try:
            # Check if we are in Tile View
            # Based on debug info: .tile-view exists when in tile view
            tile_view_indicator = page.locator(".tile-view")

            if await tile_view_indicator.count() > 0 and await tile_view_indicator.first.is_visible():
                logger.info("Detected Tile View, toggling to Speaker View via 'w' key")

                # Method 1: Keyboard shortcut 'w' (most robust)
                await page.keyboard.press("w")
                await asyncio.sleep(1)

                # Verify
                if await tile_view_indicator.count() == 0 or not await tile_view_indicator.first.is_visible():
                    logger.info("Successfully toggled to Speaker View")
                    return True

                logger.warning("Keyboard toggle failed, trying button click")

                # Method 2: Click toggle button (fallback)
                tile_button = page.locator('[aria-label*="tile" i], [aria-label*="grid" i]')
                if await tile_button.count() > 0:
                    await tile_button.first.click()
                    await asyncio.sleep(1)

                    if await tile_view_indicator.count() == 0:
                        logger.info("Successfully toggled to Speaker View via button")
                        return True
            else:
                logger.info("Already in Speaker View (no .tile-view detected)")
                return True

            logger.warning("Failed to exit Tile View")
            return False

        except Exception as e:
            logger.warning(f"Could not set layout: {e}")
            return False
