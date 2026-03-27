"""Webex Meeting Provider for Guest Join."""

import asyncio
import logging
from urllib.parse import urljoin

from playwright.async_api import FrameLocator, Page

from config.settings import get_settings
from providers.base import BaseProvider, MeetingState, MeetingStateSnapshot

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

    async def probe_state(self, page: Page) -> MeetingStateSnapshot:
        """Probe the current Webex meeting state."""
        try:
            page_title = await page.title()
        except Exception:
            page_title = ""
        title_lower = page_title.lower()

        iframe = None
        iframe_error = None
        try:
            iframe = self._get_webex_iframe(page)
        except Exception as exc:
            iframe_error = str(exc)

        in_meeting_selectors = [
            '[data-test="grid-layout"]',
            '[data-test="participants-toggle-button"]',
            '[data-test="in-meeting-chat-toggle-button"]',
            '[data-test="mc-share"]',
            '[data-test="raise-hand-button"]',
            '[data-test="more-menu-button"]',
            '[data-test$="-video-pane-container"]',
        ]
        lobby_selectors = [
            '[data-test="call_lobby_content"]',
            '[data-test="local_stream"]',
            '[data-test="pin-self-view-button"]',
        ]
        ended_selectors = [
            ':text("Meeting has ended")',
            ':text("會議已結束")',
            ':text("The host ended the meeting")',
            ':text("主持人已結束會議")',
            ':text("Meeting unavailable")',
        ]
        error_selectors = [
            '[data-test*="error"]',
            '[class*="error-dialog"]',
            '[class*="meeting-ended"]',
            '[data-test="meeting-ended"]',
        ]
        prejoin_selectors = [
            '[data-test="Name (required)"]',
            '[data-test="Email (required)"]',
            '[data-test="join-button"]',
        ]
        password_selectors = [
            '[data-test*="password" i]',
            'input[type="password"]',
            'input[placeholder*="password" i]',
        ]

        matched_in_meeting = []
        matched_lobby = []
        matched_ended = []
        matched_error = []
        matched_prejoin = []
        matched_password = []

        if iframe:
            for selector in in_meeting_selectors:
                if await iframe.locator(selector).count() > 0:
                    matched_in_meeting.append(selector)
            for selector in lobby_selectors:
                if await iframe.locator(selector).count() > 0:
                    matched_lobby.append(selector)
            for selector in ended_selectors:
                if await iframe.locator(selector).count() > 0:
                    matched_ended.append(selector)
            for selector in error_selectors:
                if await iframe.locator(selector).count() > 0:
                    matched_error.append(selector)
            for selector in prejoin_selectors:
                if await iframe.locator(selector).count() > 0:
                    matched_prejoin.append(selector)
            for selector in password_selectors:
                if await iframe.locator(selector).count() > 0:
                    matched_password.append(selector)

        if "in meeting" in title_lower or "在會議中" in page_title:
            matched_in_meeting.append("title:in_meeting")
        if "in lobby" in title_lower or "lobby" in title_lower or "大廳" in page_title:
            matched_lobby.append("title:lobby")

        if matched_in_meeting:
            return MeetingStateSnapshot(
                state=MeetingState.IN_MEETING,
                reason="Detected Webex in-meeting UI",
                evidence={"matched_selectors": matched_in_meeting, "title": page_title, "url": page.url},
            )

        if matched_ended:
            return MeetingStateSnapshot(
                state=MeetingState.ENDED,
                reason="Detected Webex meeting ended UI",
                evidence={"matched_selectors": matched_ended, "title": page_title, "url": page.url},
                error_code="MEETING_ENDED",
                error_message="Meeting ended before recording could continue",
            )

        if matched_error:
            return MeetingStateSnapshot(
                state=MeetingState.ERROR,
                reason="Detected Webex error UI",
                evidence={"matched_selectors": matched_error, "title": page_title, "url": page.url},
                error_code="MEETING_ERROR",
                error_message="Meeting error detected",
            )

        if matched_lobby:
            return MeetingStateSnapshot(
                state=MeetingState.LOBBY,
                reason="Detected Webex lobby UI",
                evidence={"matched_selectors": matched_lobby, "title": page_title, "url": page.url},
            )

        if matched_password:
            return MeetingStateSnapshot(
                state=MeetingState.ERROR,
                reason="Webex password prompt detected",
                evidence={
                    "matched_selectors": matched_password,
                    "password_prompt": True,
                    "title": page_title,
                    "url": page.url,
                },
                error_code="PASSWORD_REQUIRED",
                error_message="需要密碼",
            )

        if matched_prejoin:
            return MeetingStateSnapshot(
                state=MeetingState.PREJOIN,
                reason="Detected Webex prejoin UI",
                evidence={"matched_selectors": matched_prejoin, "title": page_title, "url": page.url},
            )

        return MeetingStateSnapshot(
            state=MeetingState.JOINING,
            reason="Waiting for Webex to transition to the next state",
            confidence=0.5,
            evidence={"title": page_title, "url": page.url, "iframe_error": iframe_error},
        )

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
