"""Webex Meeting Provider for Guest Join."""

import asyncio
import logging
from urllib.parse import urljoin

from playwright.async_api import FrameLocator, Page

from config.settings import get_settings
from providers.base import BaseProvider, JoinResult

logger = logging.getLogger(__name__)


class WebexProvider(BaseProvider):
    """Provider for Cisco Webex Meetings (Guest Join)."""

    @property
    def name(self) -> str:
        return "webex"

    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        """Build Webex guest join URL.

        Args:
            meeting_code: Meeting number or Personal Room link
            base_url: Base URL (e.g., https://company.webex.com)

        Returns:
            Full meeting URL for guest join
        """
        # If meeting_code is already a full URL, return as-is
        if meeting_code.startswith("http"):
            return meeting_code

        # Default Webex global URL
        if base_url is None:
            base_url = "https://webex.com/"

        if not base_url.endswith("/"):
            base_url += "/"

        # Meeting number format: join URL
        if meeting_code.isdigit():
            return f"{base_url}meet/j.php?MTID={meeting_code}"

        # Personal Room format
        return urljoin(base_url, f"meet/{meeting_code}")

    def _get_webex_iframe(self, page: Page) -> FrameLocator:
        """Get the Webex iframe content frame."""
        return page.locator("#unified-webclient-iframe").content_frame

    async def prejoin(
        self,
        page: Page,
        display_name: str,
        password: str | None = None,
    ) -> None:
        """Handle Webex guest join page.

        Args:
            page: Playwright page instance
            display_name: Name to display in meeting
            password: Meeting password if required
        """
        logger.info(f"Handling Webex prejoin page, display_name={display_name}")

        # Wait for page to load
        await asyncio.sleep(2)

        # Handle cookie consent banner (on main page)
        try:
            accept_btn = page.get_by_role("button", name="Accept")
            if await accept_btn.count() > 0:
                await accept_btn.first.click()
                logger.info("Accepted cookie consent")
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # Click "Join from this browser" button (on main page) if present
        # This might be skipped if cookies remember the choice
        join_browser_selectors = [
            page.get_by_role("button", name="Join from this browser"),
            page.get_by_role("button", name="Join from browser"),
            page.locator('button:has-text("Join from this browser")'),
            page.locator('button:has-text("Join from browser")'),
            page.locator('button:has-text("從瀏覽器加入")'),
            page.locator('[data-test="join-browser-button"]'),
        ]

        join_clicked = False
        for selector in join_browser_selectors:
            try:
                if await selector.count() > 0:
                    await selector.first.click()
                    logger.info("Clicked 'Join from this browser' button")
                    join_clicked = True
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

        if not join_clicked:
            logger.info("No 'Join from browser' button found, assuming already on prejoin page")

        # Wait for iframe to load (this might already be present)
        iframe_locator = page.locator("#unified-webclient-iframe")
        try:
            await iframe_locator.wait_for(state="visible", timeout=15000)
        except Exception:
            # Try waiting a bit more
            await asyncio.sleep(3)
            if await iframe_locator.count() == 0:
                logger.error("Could not find Webex iframe")
                raise RuntimeError("Webex iframe not found")
        await asyncio.sleep(2)  # Wait for iframe content to initialize

        # Now work within the iframe
        iframe = self._get_webex_iframe(page)

        # First try to close any permission dialogs (may not appear with fake-ui)
        try:
            close_dialog_btn = iframe.get_by_role("button", name="Close dialog")
            if await close_dialog_btn.count() > 0:
                await close_dialog_btn.click()
                logger.info("Closed camera dialog")
                await asyncio.sleep(0.5)
        except Exception:
            pass

        try:
            reject_btn = iframe.get_by_role("button", name="Reject")
            if await reject_btn.count() > 0:
                await reject_btn.click()
                logger.info("Rejected microphone permission")
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # Explicitly disable video by clicking the video button if it's ON
        # Look for video mute button and click it to turn OFF
        video_off_selectors = [
            '[data-test="video-button"]',
            '[aria-label*="Stop video" i]',
            '[aria-label*="Turn off camera" i]',
            '[aria-label*="關閉視訊" i]',
            '[aria-label*="停止視訊" i]',
            'button[data-test*="video" i]',
        ]

        for selector in video_off_selectors:
            try:
                video_btn = iframe.locator(selector)
                if await video_btn.count() > 0:
                    # Check if video is currently ON (button would say "Stop" or similar)
                    aria_label = await video_btn.first.get_attribute("aria-label") or ""
                    if (
                        "stop" in aria_label.lower()
                        or "off" in aria_label.lower()
                        or "關閉" in aria_label
                        or "停止" in aria_label
                    ):
                        await video_btn.first.click()
                        logger.info(f"Disabled video using: {selector}")
                        await asyncio.sleep(0.5)
                        break
                    # Also try clicking if aria-label suggests video is active
                    elif "start" not in aria_label.lower() and "開啟" not in aria_label:
                        await video_btn.first.click()
                        logger.info(f"Toggled video using: {selector}")
                        await asyncio.sleep(0.5)
                        break
            except Exception as e:
                logger.debug(f"Could not click video button {selector}: {e}")
                continue

        # Disable microphone similarly
        mic_off_selectors = [
            '[data-test="mute-button"]',
            '[aria-label*="Mute" i]:not([aria-label*="Unmute" i])',
            '[aria-label*="靜音" i]:not([aria-label*="取消" i])',
            'button[data-test*="mute" i]',
        ]

        for selector in mic_off_selectors:
            try:
                mic_btn = iframe.locator(selector)
                if await mic_btn.count() > 0:
                    aria_label = await mic_btn.first.get_attribute("aria-label") or ""
                    if "mute" in aria_label.lower() and "unmute" not in aria_label.lower():
                        await mic_btn.first.click()
                        logger.info(f"Muted mic using: {selector}")
                        await asyncio.sleep(0.5)
                        break
                    elif "靜音" in aria_label and "取消" not in aria_label:
                        await mic_btn.first.click()
                        logger.info(f"Muted mic using: {selector}")
                        await asyncio.sleep(0.5)
                        break
            except Exception as e:
                logger.debug(f"Could not click mic button {selector}: {e}")
                continue

        # Wait for name input to be visible and fill it
        name_selectors = [
            '[data-test="Name (required)"]',
            'input[placeholder*="name" i]',
            'input[name="name"]',
        ]

        for selector in name_selectors:
            name_input = iframe.locator(selector)
            if await name_input.count() > 0:
                await name_input.first.click()
                await name_input.first.fill(display_name)
                logger.info(f"Filled name using: {selector}")
                break
        else:
            logger.warning("Could not find name input")

        # Fill email if present
        settings = get_settings()
        guest_email = getattr(settings, "webex_guest_email", None) or "recorder@example.com"
        email_selectors = [
            '[data-test="Email (required)"]',
            'input[type="email"]',
            'input[placeholder*="email" i]',
        ]
        for selector in email_selectors:
            email_input = iframe.locator(selector)
            if await email_input.count() > 0:
                await email_input.click()
                await email_input.fill(guest_email)
                logger.info(f"Filled email: {guest_email}")
                break

    async def click_join(self, page: Page) -> None:
        """Click the join meeting button.

        Args:
            page: Playwright page instance
        """
        logger.info("Clicking Webex join button")

        try:
            iframe = self._get_webex_iframe(page)

            # Primary selector from recorded action
            join_btn = iframe.locator('[data-test="join-button"]')
            if await join_btn.count() > 0:
                await join_btn.click()
                logger.info("Clicked join button [data-test='join-button']")
                return

            # Alternative selectors
            alt_selectors = [
                iframe.get_by_role("button", name="Join"),
                iframe.get_by_role("button", name="Join meeting"),
                iframe.locator('button:has-text("Join")'),
                iframe.locator('button:has-text("加入")'),
            ]

            for btn in alt_selectors:
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info("Clicked join button (alternative)")
                    return

        except Exception as e:
            logger.warning(f"Error clicking join in iframe: {e}")

        # Fallback: try on main page
        try:
            main_join_selectors = [
                'button:has-text("Join")',
                'button:has-text("Join meeting")',
                'button[type="submit"]',
            ]
            for selector in main_join_selectors:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    logger.info(f"Clicked join button on main page: {selector}")
                    return
        except Exception as e:
            logger.warning(f"Fallback join also failed: {e}")

        raise RuntimeError("Could not find Webex join button")

    async def apply_password(self, page: Page, password: str) -> bool:
        """Apply password when prompted.

        Args:
            page: Playwright page instance
            password: Meeting password

        Returns:
            True if password was entered successfully
        """
        logger.info("Checking for Webex password dialog")

        await asyncio.sleep(1)

        try:
            iframe = self._get_webex_iframe(page)

            password_selectors = [
                '[data-test*="password" i]',
                'input[type="password"]',
                'input[placeholder*="password" i]',
            ]

            for selector in password_selectors:
                password_input = iframe.locator(selector)
                if await password_input.count() > 0:
                    await password_input.fill(password)
                    logger.info("Password filled in dialog")

                    # Find submit button
                    submit_btn = iframe.locator(
                        '[data-test="submit-button"], button:has-text("OK"), button:has-text("Submit")'
                    )
                    if await submit_btn.count() > 0:
                        await submit_btn.first.click()
                        logger.info("Password submitted")
                        return True

                    await password_input.press("Enter")
                    return True

        except Exception as e:
            logger.debug(f"No password dialog in iframe: {e}")

        # Try main page
        try:
            password_input = page.locator('input[type="password"]')
            if await password_input.count() > 0:
                await password_input.fill(password)
                await password_input.press("Enter")
                return True
        except Exception as e:
            logger.debug(f"No password on main page: {e}")

        return False

    async def wait_until_joined(self, page: Page, timeout_sec: int = 60, password: str | None = None) -> JoinResult:
        """Wait until successfully joined the Webex meeting.

        Args:
            page: Playwright page instance
            timeout_sec: Maximum time to wait
            password: Optional password to apply if prompted

        Returns:
            JoinResult with success status
        """
        logger.info(f"Waiting to join Webex meeting (timeout={timeout_sec}s)")

        start_time = asyncio.get_event_loop().time()
        end_time = start_time + timeout_sec
        password_attempted = False

        while asyncio.get_event_loop().time() < end_time:
            try:
                iframe = self._get_webex_iframe(page)

                # Check if in meeting (look for meeting UI elements)
                meeting_indicators = [
                    '[data-test="participants-toggle-button"]',
                    '[data-test="meeting-info-container"]',
                    '[data-test="leave-button"]',
                    '[class*="meeting-container"]',
                ]

                for indicator in meeting_indicators:
                    if await iframe.locator(indicator).count() > 0:
                        logger.info("Successfully joined Webex meeting")
                        return JoinResult(success=True, in_lobby=False)

                # Check for lobby/waiting room
                lobby_indicators = [
                    ':text("Waiting for host")',
                    ':text("等待主持人")',
                    ':text("waiting room")',
                    ':text("等候室")',
                    ':text("Let you in")',
                    ':text("allow you to join")',
                    '[data-test="lobby-container"]',
                ]

                for indicator in lobby_indicators:
                    if await iframe.locator(indicator).count() > 0:
                        logger.info("Detected Webex lobby/waiting room")
                        return JoinResult(success=False, in_lobby=True)

                # Check for password prompt
                if password and not password_attempted:
                    password_input = iframe.locator('input[type="password"]:visible')
                    if await password_input.count() > 0:
                        if await self.apply_password(page, password):
                            password_attempted = True
                            await asyncio.sleep(2)
                            continue

                # Check for errors
                error_checks = [
                    (':text("Meeting not found")', "MEETING_NOT_FOUND", "會議不存在"),
                    (':text("Invalid meeting")', "MEETING_NOT_FOUND", "無效會議"),
                    (':text("Meeting has ended")', "MEETING_ENDED", "會議已結束"),
                    (':text("會議已結束")', "MEETING_ENDED", "會議已結束"),
                    (':text("Incorrect password")', "PASSWORD_INCORRECT", "密碼錯誤"),
                    (':text("meeting is locked")', "MEETING_LOCKED", "會議已鎖定"),
                ]

                for indicator, error_code, error_msg in error_checks:
                    if await iframe.locator(indicator).count() > 0:
                        logger.error(f"Webex join error: {error_msg}")
                        return JoinResult(
                            success=False,
                            in_lobby=False,
                            error_code=error_code,
                            error_message=error_msg,
                        )

            except Exception as e:
                logger.debug(f"Error checking meeting status: {e}")

            await asyncio.sleep(1)

        logger.error("Timeout waiting to join Webex meeting")
        return JoinResult(
            success=False,
            in_lobby=False,
            error_code="JOIN_TIMEOUT",
            error_message=f"Timeout after {timeout_sec} seconds",
        )

    async def wait_in_lobby(self, page: Page, max_wait_sec: int = 900) -> bool:
        """Wait in Webex lobby until admitted.

        Args:
            page: Playwright page instance
            max_wait_sec: Maximum wait time (default 15 minutes)

        Returns:
            True if admitted, False if timeout
        """
        logger.info(f"Waiting in Webex lobby (max={max_wait_sec}s)")

        start_time = asyncio.get_event_loop().time()
        end_time = start_time + max_wait_sec
        check_interval = 5

        while asyncio.get_event_loop().time() < end_time:
            try:
                iframe = self._get_webex_iframe(page)

                # Check if now in meeting
                meeting_indicators = [
                    '[data-test="participants-toggle-button"]',
                    '[data-test="leave-button"]',
                    '[class*="meeting-container"]',
                ]

                for indicator in meeting_indicators:
                    if await iframe.locator(indicator).count() > 0:
                        logger.info("Admitted from Webex lobby")
                        return True

                # Check if rejected
                rejected_indicators = [
                    ':text("rejected")',
                    ':text("denied")',
                    ':text("removed")',
                    ':text("拒絕")',
                ]

                for indicator in rejected_indicators:
                    if await iframe.locator(indicator).count() > 0:
                        logger.error("Rejected from Webex lobby")
                        return False

                # Check if meeting ended
                ended_indicators = [
                    ':text("Meeting has ended")',
                    ':text("會議已結束")',
                ]

                for indicator in ended_indicators:
                    if await iframe.locator(indicator).count() > 0:
                        logger.info("Meeting ended while in lobby")
                        return False

            except Exception as e:
                logger.debug(f"Error checking lobby status: {e}")

            elapsed = int(asyncio.get_event_loop().time() - start_time)
            if elapsed % 60 == 0 and elapsed > 0:
                logger.info(f"Still waiting in Webex lobby... ({elapsed}s elapsed)")

            await asyncio.sleep(check_interval)

        logger.error(f"Webex lobby timeout after {max_wait_sec}s")
        return False

    async def set_layout(self, page: Page, preset: str = "speaker") -> bool:
        """Attempt to set Webex video layout.

        Args:
            page: Playwright page instance
            preset: Layout preset (speaker or gallery)

        Returns:
            True if layout was set
        """
        logger.info(f"Attempting to set Webex layout: {preset}")

        try:
            iframe = self._get_webex_iframe(page)

            # Look for layout/view options
            layout_btn = iframe.locator('[data-test="layout-button"], button[aria-label*="layout" i]')
            if await layout_btn.count() > 0:
                await layout_btn.first.click()
                await asyncio.sleep(0.5)

                if preset == "speaker":
                    speaker_option = iframe.locator(':text("Speaker view"), :text("發言人檢視")')
                else:
                    speaker_option = iframe.locator(':text("Grid view"), :text("格狀檢視")')

                if await speaker_option.count() > 0:
                    await speaker_option.first.click()
                    logger.info(f"Layout set to {preset}")
                    return True

            return True
        except Exception as e:
            logger.warning(f"Could not set Webex layout: {e}")
            return False

    async def detect_meeting_end(self, page: Page) -> bool:
        """Check if Webex meeting has ended.

        Args:
            page: Playwright page instance

        Returns:
            True if meeting ended
        """
        try:
            iframe = self._get_webex_iframe(page)

            end_indicators = [
                ':text("Meeting has ended")',
                ':text("會議已結束")',
                ':text("You have left the meeting")',
                ':text("已離開會議")',
                ':text("disconnected")',
                ':text("連線已中斷")',
            ]

            for indicator in end_indicators:
                if await iframe.locator(indicator).count() > 0:
                    logger.info("Webex meeting end detected")
                    return True

        except Exception as e:
            # If iframe is gone, meeting probably ended
            logger.debug(f"Error checking meeting end (iframe may be gone): {e}")

        # Check URL change
        url = page.url
        if "webex.com" not in url:
            logger.info("Navigated away from Webex")
            return True

        return False
