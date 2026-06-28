"""Webex Meeting Provider for Guest Join."""

import logging
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from playwright.async_api import FrameLocator, Page

from config.settings import get_settings
from providers.base import BaseProvider, JoinResult, MeetingState, MeetingStateSnapshot

logger = logging.getLogger(__name__)

WEBEX_PREJOIN_CONTROL_SELECTORS = [
    '[data-test="Name (required)"]',
    'mdc-input[data-test="名稱"]',
    'mdc-input[data-test*="name" i]',
    'mdc-input[label="名稱"]',
    'mdc-input[label*="name" i]',
    '[data-test="Email (required)"]',
    '[data-test="join-button"]',
    '[data-test="video-button"]',
    '[data-test="camera-button"]',
    '[data-test="mute-button"]',
    '[data-test="microphone-button"]',
]


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

    async def _webex_client_targets(self, page: Page) -> list:
        """Return possible Webex web-client DOM targets, preferring the embedded iframe."""
        targets = []
        try:
            if await page.locator("#unified-webclient-iframe").count() > 0:
                targets.append(self._get_webex_iframe(page))
        except Exception:
            pass
        targets.append(page)
        return targets

    async def _locator_still_visible(self, locator) -> bool:
        try:
            if hasattr(locator, "is_visible"):
                return await locator.is_visible()
        except Exception:
            return False
        return False

    async def _locator_label_text(self, locator) -> str:
        """Return aria/text evidence for a Webex control."""
        parts = []
        for attr in ("aria-label", "data-aria-label", "title"):
            try:
                value = await locator.get_attribute(attr)
                if value:
                    parts.append(value)
            except Exception:
                pass
        text_content = getattr(locator, "text_content", None)
        if text_content:
            try:
                value = await text_content()
                if value:
                    parts.append(value)
            except Exception:
                pass
        return " ".join(parts).strip()

    async def _activate_visible_locator(self, locator, description: str) -> None:
        """Activate a visible Webex control with fallbacks for modal buttons."""
        click_attempts = [
            ("click", lambda: locator.click()),
            ("force click", lambda: locator.click(force=True)),
            ("press Enter", lambda: locator.press("Enter")),
        ]

        evaluate = getattr(locator, "evaluate", None)
        if evaluate:
            click_attempts.append(
                (
                    "DOM click",
                    lambda: evaluate(
                        """(element) => {
                            const clickable = element.matches('button, [role="button"]')
                                ? element
                                : (element.shadowRoot && element.shadowRoot.querySelector('button, [role="button"]'))
                                    || element.querySelector('button, [role="button"]')
                                    || element;
                            clickable.click();
                        }"""
                    ),
                )
            )

        last_error = None
        for label, click_attempt in click_attempts:
            try:
                await click_attempt()
                logger.info("Clicked Webex %s via %s", description, label)
                await self.short_ui_settle(fallback_sec=0.3, reason=f"Webex {description} {label}")
                if not await self._locator_still_visible(locator):
                    return
            except Exception as exc:
                last_error = exc
                logger.debug("Could not activate Webex %s via %s: %s", description, label, exc)

        if last_error:
            raise last_error

    async def _click_first_visible(self, locators: list, description: str, *, robust_activation: bool = False) -> bool:
        """Click the first visible locator across a candidate list."""
        for locator in locators:
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 8)):
                try:
                    candidate = locator.nth(index) if count > 1 else self._first_locator(locator)
                    if hasattr(candidate, "is_visible") and not await candidate.is_visible():
                        continue
                    if robust_activation:
                        await self._activate_visible_locator(candidate, description)
                    else:
                        await candidate.click()
                        logger.info("Clicked Webex %s", description)
                    return True
                except Exception as exc:
                    logger.debug("Could not click Webex %s candidate %s: %s", description, index, exc)
                    continue
        return False

    async def _fill_first_visible(self, locators: list, value: str, description: str) -> bool:
        """Fill the first visible input-like locator, including Webex web components."""
        for locator in locators:
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 8)):
                candidate = locator.nth(index) if count > 1 else self._first_locator(locator)
                try:
                    if hasattr(candidate, "is_visible") and not await candidate.is_visible():
                        continue
                    await candidate.click()
                    await candidate.fill(value)
                    logger.info("Filled Webex %s", description)
                    return True
                except Exception as exc:
                    evaluate = getattr(candidate, "evaluate", None)
                    if not evaluate:
                        logger.debug("Could not fill Webex %s candidate %s: %s", description, index, exc)
                        continue
                    try:
                        filled = await evaluate(
                            """(element, value) => {
                                const input = element.matches('input, textarea')
                                    ? element
                                    : (element.shadowRoot && element.shadowRoot.querySelector('input, textarea'))
                                        || element.querySelector('input, textarea');
                                const target = input || element;
                                if (!('value' in target)) {
                                    return false;
                                }
                                target.value = value;
                                target.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                                target.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
                                return true;
                            }""",
                            value,
                        )
                        if filled:
                            logger.info("Filled Webex %s via DOM fallback", description)
                            return True
                    except Exception as fallback_exc:
                        logger.debug(
                            "Could not fill Webex %s candidate %s via DOM fallback: %s",
                            description,
                            index,
                            fallback_exc,
                        )
        return False

    async def _dismiss_prejoin_dialogs(self, target) -> bool:
        """Dismiss localized Webex prejoin helper dialogs that can cover the join button."""
        locators = []
        get_by_role = getattr(target, "get_by_role", None)
        if get_by_role:
            locators.extend(
                [
                    get_by_role("button", name=re.compile(r"^(Close dialog|Close|關閉|關閉對話方塊)$", re.I)),
                    get_by_role("button", name=re.compile(r"(Close|關閉)", re.I)),
                ]
            )
        locators.extend(
            [
                target.locator('[aria-label*="Close" i]:visible'),
                target.locator('[aria-label*="關閉"]:visible'),
                target.locator('[role="dialog"]:visible button:has-text("Reject")'),
                target.locator('[role="dialog"]:visible button:has-text("拒絕")'),
                target.locator('[role="dialog"]:visible mdc-button:has-text("Reject")'),
                target.locator('[role="dialog"]:visible mdc-button:has-text("拒絕")'),
                target.locator('button:has-text("Close"):visible'),
                target.locator('button:has-text("關閉"):visible'),
                target.locator('mdc-button:has-text("Close"):visible'),
                target.locator('mdc-button:has-text("關閉"):visible'),
            ]
        )
        dismissed = await self._click_first_visible(locators, "prejoin dialog dismissal")
        if dismissed:
            await self.short_ui_settle(fallback_sec=0.3, reason="Webex prejoin dialog dismissal")
        return dismissed

    async def _accept_cookie_banner(self, target) -> bool:
        """Accept Webex cookies when the localized banner is visible."""
        try:
            locators = [
                target.locator('#cookieMgmtbanner:visible button:has-text("Accept")'),
                target.locator('#cookieMgmtbanner:visible button:has-text("接受")'),
                target.locator('#cookieMgmtbanner:visible button:has-text("Allow all")'),
                target.locator('#cookieMgmtbanner:visible button:has-text("同意")'),
            ]
            get_by_role = getattr(target, "get_by_role", None)
            if get_by_role:
                locators.append(get_by_role("button", name=re.compile(r"^(Accept|接受|Allow all|同意)$", re.I)))
            locators.extend(
                [
                    target.locator('button:has-text("Accept"):visible'),
                    target.locator('button:has-text("接受"):visible'),
                    target.locator('button:has-text("Allow all"):visible'),
                    target.locator('button:has-text("同意"):visible'),
                    target.locator('mdc-button:has-text("Accept"):visible'),
                    target.locator('mdc-button:has-text("接受"):visible'),
                    target.locator('mdc-button:has-text("Allow all"):visible'),
                    target.locator('mdc-button:has-text("同意"):visible'),
                ]
            )
            accepted = await self._click_first_visible(
                locators,
                "cookie consent",
                robust_activation=True,
            )
            if accepted:
                await self.short_ui_settle(fallback_sec=0.2, reason="Webex cookie consent")
            return accepted
        except Exception:
            return False

    async def _accept_cookie_banner_everywhere(self, page: Page) -> None:
        """Accept cookie banners on both the landing page and Webex client target."""
        await self._accept_cookie_banner(page)
        for target in await self._webex_client_targets(page):
            if target is page:
                continue
            await self._accept_cookie_banner(target)

    async def _turn_off_camera_if_needed(self, target) -> bool:
        """Turn off a live Webex prejoin camera preview without clicking start-video controls."""
        selectors = [
            '[data-test="camera-button"]',
            '[data-test="video-button"]',
            '[aria-label*="Stop video" i]',
            '[aria-label*="Turn off camera" i]',
            '[aria-label*="關閉視訊" i]',
            '[aria-label*="停止視訊" i]',
            'button[data-test*="video" i]',
            'mdc-button[data-test*="camera" i]',
        ]
        active_phrases = (
            "stop video",
            "turn off",
            "currently on",
            "camera is on",
            "關閉視訊",
            "停止視訊",
        )
        for selector in selectors:
            locator = target.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 4)):
                candidate = locator.nth(index) if count > 1 else self._first_locator(locator)
                try:
                    if hasattr(candidate, "is_visible") and not await candidate.is_visible():
                        continue
                    label = (await self._locator_label_text(candidate)).lower()
                    if not label or not any(phrase in label for phrase in active_phrases):
                        continue
                    await candidate.click()
                    logger.info("Turned off Webex camera using: %s", selector)
                    await self.short_ui_settle(fallback_sec=0.2, reason="Webex camera toggle")
                    return True
                except Exception as exc:
                    logger.debug("Could not inspect/click Webex camera button %s: %s", selector, exc)
        return False

    async def _mute_microphone_if_needed(self, target) -> bool:
        """Mute a Webex prejoin microphone only when the current state is unmuted."""
        selectors = [
            '[data-test="microphone-button"]',
            '[data-test="mute-button"]',
            '[aria-label*="Mute" i]',
            '[aria-label*="靜音" i]',
            'button[data-test*="mute" i]',
            'mdc-button[data-test*="microphone" i]',
        ]
        for selector in selectors:
            locator = target.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 4)):
                candidate = locator.nth(index) if count > 1 else self._first_locator(locator)
                try:
                    if hasattr(candidate, "is_visible") and not await candidate.is_visible():
                        continue
                    label = (await self._locator_label_text(candidate)).lower()
                    if not label:
                        continue
                    should_click = (
                        "click to mute" in label
                        or "currently unmuted" in label
                        or label.strip() == "mute"
                        or ("靜音" in label and "取消" not in label)
                    )
                    if not should_click:
                        continue
                    await candidate.click()
                    logger.info("Muted Webex microphone using: %s", selector)
                    await self.short_ui_settle(fallback_sec=0.2, reason="Webex mic toggle")
                    return True
                except Exception as exc:
                    logger.debug("Could not inspect/click Webex microphone button %s: %s", selector, exc)
        return False

    async def _quiet_prejoin_media(self, target) -> None:
        """Best-effort mute/camera-off for Webex prejoin media controls."""
        await self.wait_for_any_selector(
            target,
            [
                '[data-test="microphone-button"]',
                '[data-test="camera-button"]',
                '[data-test="mute-button"]',
                '[data-test="video-button"]',
            ],
            state="attached",
            timeout_ms=1500,
            fallback_sec=0.2,
            reason="Webex prejoin media controls",
        )
        await self._mute_microphone_if_needed(target)
        await self._turn_off_camera_if_needed(target)

    async def _click_visible_browser_join(self, page: Page) -> bool:
        """Advance Webex download/browser-choice pages through the visible browser join control."""
        return await self._click_first_visible(
            [
                # Live Webex sites can show this timing modal before exposing the real web-client prejoin UI.
                page.locator('[role="dialog"]:visible #fallBkJoinByBrowser:has-text("Join from browser")'),
                page.locator('[role="dialog"]:visible #fallBkJoinByBrowser:has-text("從瀏覽器加入")'),
                page.locator("#fallBkJoinByBrowser:visible"),
                page.locator('[role="dialog"]:visible button:has-text("Join from browser")'),
                page.locator('[role="dialog"]:visible button:has-text("Join from this browser")'),
                page.locator('[role="dialog"]:visible button:has-text("從瀏覽器加入")'),
                page.locator('[role="dialog"]:visible button:has-text("從此瀏覽器加入")'),
                page.locator("#broadcom-center-right:visible"),
                page.locator('[aria-label="Join from this browser"]:visible'),
                page.locator('[aria-label="Join from browser"]:visible'),
                page.locator('[aria-label="從此瀏覽器加入"]:visible'),
                page.locator('[aria-label="從瀏覽器加入"]:visible'),
                page.locator('[data-test="join-browser-button"]:visible'),
                page.locator('button:has-text("Join from this browser"):visible'),
                page.locator('button:has-text("Join from browser"):visible'),
                page.locator('button:has-text("從此瀏覽器加入"):visible'),
                page.locator('button:has-text("從瀏覽器加入"):visible'),
            ],
            "browser join control",
            robust_activation=True,
        )

    async def _browser_join_prompt_visible(self, page: Page) -> bool:
        """Return whether Webex is still showing a browser-choice prompt."""
        prompt_selectors = [
            '[role="dialog"]:visible #fallBkJoinByBrowser:has-text("Join from browser")',
            '[role="dialog"]:visible #fallBkJoinByBrowser:has-text("從瀏覽器加入")',
            '[role="dialog"]:visible button:has-text("Join from browser")',
            '[role="dialog"]:visible button:has-text("從瀏覽器加入")',
            "#broadcom-center-right:visible",
        ]
        for selector in prompt_selectors:
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _direct_web_client_url(self, iframe_src: str | None) -> str | None:
        """Convert Webex's hidden preload iframe URL into a top-level web-client URL."""
        if not iframe_src:
            return None
        parsed = urlsplit(iframe_src)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None

        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "preload"]
        query.append(("preload", "false"))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    async def _open_preloaded_web_client(self, page: Page) -> bool:
        """Navigate directly to Webex's preloaded web client when the landing modal will not advance."""
        try:
            iframe_src = await page.locator("#unified-webclient-iframe").get_attribute("src")
        except Exception:
            return False

        web_client_url = self._direct_web_client_url(iframe_src)
        if not web_client_url:
            return False

        logger.info("Opening Webex preloaded web client directly")
        await page.goto(web_client_url, wait_until="domcontentloaded")
        await self.wait_for_page_idle(page, timeout_ms=5000, fallback_sec=0.5, reason="Webex direct web client")
        return True

    async def _wait_for_prejoin_controls(self, page: Page, *, timeout_ms: int = 8000):
        """Return the Webex client target after the real prejoin controls are attached."""
        targets = await self._webex_client_targets(page)
        controls_match = await self.wait_for_any_selector(
            targets,
            WEBEX_PREJOIN_CONTROL_SELECTORS,
            state="attached",
            timeout_ms=timeout_ms,
            fallback_sec=0.2,
            reason="Webex prejoin controls",
        )
        if controls_match:
            return controls_match.target

        iframe_locator = page.locator("#unified-webclient-iframe")
        iframe_match = await self.wait_for_any_selector(
            page,
            ["#unified-webclient-iframe"],
            state="attached",
            timeout_ms=timeout_ms,
            reason="Webex iframe",
        )
        if not iframe_match and await iframe_locator.count() == 0:
            return None

        iframe = self._get_webex_iframe(page)
        controls_match = await self.wait_for_any_selector(
            iframe,
            WEBEX_PREJOIN_CONTROL_SELECTORS,
            state="attached",
            timeout_ms=timeout_ms,
            fallback_sec=0.2,
            reason="Webex iframe controls",
        )
        return iframe if controls_match else None

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

        # Prefer bounded Playwright readiness over fixed startup sleep.
        await self.wait_for_page_idle(page, timeout_ms=3000, fallback_sec=0.5, reason="Webex prejoin page")

        # Handle cookie consent banner (on main page)
        await self._accept_cookie_banner(page)

        client_target = await self._wait_for_prejoin_controls(page, timeout_ms=3000)
        if client_target is None:
            for attempt in range(3):
                if not await self._click_visible_browser_join(page):
                    if attempt == 0:
                        logger.info("No visible Webex browser join control found, assuming already on prejoin page")
                    break
                await self.wait_for_page_idle(
                    page, timeout_ms=3000, fallback_sec=0.5, reason="Webex browser join transition"
                )
                client_target = await self._wait_for_prejoin_controls(page, timeout_ms=8000)
                if client_target is not None:
                    break

        if client_target is None and await self._browser_join_prompt_visible(page):
            if await self._open_preloaded_web_client(page):
                client_target = await self._wait_for_prejoin_controls(page, timeout_ms=12000)

        if client_target is None:
            if await self._browser_join_prompt_visible(page):
                logger.error("Webex browser join prompt remained visible after activation attempts")
                raise RuntimeError("Webex browser join prompt did not advance")
            if await page.locator("#unified-webclient-iframe").count() == 0:
                logger.error("Could not find Webex iframe")
                raise RuntimeError("Webex iframe not found")
            client_target = self._get_webex_iframe(page)
            logger.warning("Webex iframe controls did not become ready before prejoin form handling")

        # First try to close any permission/helper dialogs (may not appear with fake-ui).
        await self._accept_cookie_banner(client_target)
        await self._dismiss_prejoin_dialogs(client_target)
        await self._quiet_prejoin_media(client_target)

        # Wait for name input to be visible and fill it
        name_selectors = [
            '[data-test="Name (required)"]',
            'mdc-input[data-test="名稱"] input',
            'mdc-input[data-test="名稱"]',
            'mdc-input[data-test*="name" i] input',
            'mdc-input[data-test*="name" i]',
            'mdc-input[label="名稱"] input',
            'mdc-input[label="名稱"]',
            'mdc-input[label*="name" i] input',
            'mdc-input[label*="name" i]',
            "mdc-input[required] input",
            "mdc-input[required]",
            'input[aria-label*="name" i]',
            'input[aria-label*="名稱"]',
            'input[placeholder*="name" i]',
            'input[placeholder*="名稱"]',
            'input[name="name"]',
        ]

        if not await self._fill_first_visible(
            [client_target.locator(selector) for selector in name_selectors],
            display_name,
            "display name",
        ):
            logger.warning("Could not find name input")
        else:
            await self._dismiss_prejoin_dialogs(client_target)
            await self.wait_for_any_selector(
                client_target,
                [
                    '[data-test="join-button"]:not([disabled]):not([aria-disabled="true"])',
                    '#join-button:not([disabled]):not([aria-disabled="true"])',
                    'mdc-button[data-test="join-button"]:not([disabled]):not([aria-disabled="true"])',
                ],
                state="attached",
                timeout_ms=3000,
                fallback_sec=0.5,
                reason="Webex enabled join button",
            )
            await self._quiet_prejoin_media(client_target)

        # Fill email if present
        settings = get_settings()
        guest_email = getattr(settings, "webex_guest_email", None) or "recorder@example.com"
        email_selectors = [
            '[data-test="Email (required)"]',
            'input[type="email"]',
            'input[placeholder*="email" i]',
        ]
        for selector in email_selectors:
            email_input = client_target.locator(selector)
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
        await self._accept_cookie_banner_everywhere(page)

        if await self._browser_join_prompt_visible(page):
            raise RuntimeError("Webex browser join prompt still visible before final join")

        targets = await self._webex_client_targets(page)
        for target in targets:
            await self._dismiss_prejoin_dialogs(target)
            await self._quiet_prejoin_media(target)

        for target in targets:
            try:
                join_selectors = []
                get_by_role = getattr(target, "get_by_role", None)
                if get_by_role:
                    join_selectors.append(
                        get_by_role("button", name=re.compile(r"^(Join|Join meeting|加入|加入會議)$", re.I))
                    )
                join_selectors.extend(
                    [
                        target.locator('button:has-text("Join meeting"):visible'),
                        target.locator('button:has-text("Join"):visible'),
                        target.locator('button:has-text("加入會議"):visible'),
                        target.locator('button:has-text("加入"):visible'),
                        target.locator('mdc-button:has-text("Join meeting"):visible'),
                        target.locator('mdc-button:has-text("Join"):visible'),
                        target.locator('mdc-button:has-text("加入會議"):visible'),
                        target.locator('mdc-button:has-text("加入"):visible'),
                        target.locator('[data-test="join-button"]:not([disabled]):not([aria-disabled="true"])'),
                        target.locator(
                            'mdc-button[data-test="join-button"]:not([disabled]):not([aria-disabled="true"])'
                        ),
                    ]
                )

                if await self._click_first_visible(
                    join_selectors,
                    "Webex client join button",
                    robust_activation=True,
                ):
                    logger.info("Clicked Webex join button")
                    return
            except Exception as e:
                logger.debug(f"Error clicking join in Webex client target: {e}")

        # Fallback: try on main page
        try:
            main_join_selectors = [
                page.locator('button:has-text("Join meeting"):visible'),
                page.locator('button:has-text("Join"):visible'),
                page.locator('button:has-text("加入會議"):visible'),
                page.locator('button:has-text("加入"):visible'),
                page.locator('button[type="submit"]:visible'),
            ]
            if await self._click_first_visible(main_join_selectors, "main page join button"):
                logger.info("Clicked join button on main page")
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

        password_selectors = [
            '[data-test*="password" i]',
            'input[type="password"]',
            'input[placeholder*="password" i]',
        ]

        try:
            targets = await self._webex_client_targets(page)
            await self.wait_for_any_selector(
                targets,
                password_selectors,
                timeout_ms=1500,
                reason="Webex password dialog",
            )

            submit_selectors = [
                '[data-test="submit-button"]',
                'button:has-text("OK")',
                'button:has-text("Submit")',
                'button:has-text("確定")',
                'button:has-text("送出")',
            ]
            for target in targets:
                for selector in password_selectors:
                    password_input = target.locator(selector)
                    if await password_input.count() <= 0:
                        continue
                    await password_input.fill(password)
                    logger.info("Password filled in dialog")

                    if await self._click_first_visible(
                        [target.locator(submit_selector) for submit_selector in submit_selectors],
                        "password submit button",
                    ):
                        logger.info("Password submitted")
                        return True

                    await password_input.press("Enter")
                    return True

        except Exception as e:
            logger.debug(f"No password dialog in Webex client target: {e}")

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

        iframe_error = None
        try:
            targets = await self._webex_client_targets(page)
        except Exception as exc:
            iframe_error = str(exc)
            targets = [page]

        in_meeting_selectors = [
            '[data-test="grid-layout"]',
            '[data-test="participants-toggle-button"]',
            '[data-test="in-meeting-chat-toggle-button"]',
            '[data-test="mc-share"]',
            '[data-test="raise-hand-button"]',
        ]
        lobby_selectors = [
            '[data-test="call_lobby_content"]',
            '[data-test*="lobby" i]',
            ':text("Waiting in lobby")',
            ':text("Waiting for the host")',
            ':text("The host will let you in")',
            ':text("You are in the lobby")',
            ':text("正在等候主持人")',
            ':text("等待主持人")',
            ':text("大廳")',
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
            'mdc-input[data-test="名稱"]',
            'mdc-input[data-test*="name" i]',
            'mdc-input[label="名稱"]',
            'mdc-input[label*="name" i]',
            '[data-test="Email (required)"]',
            '[data-test="join-button"]',
        ]
        joining_selectors = [
            ':text("正在連線")',
            ':text("Connecting")',
            ':text("Joining")',
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
        matched_joining = []
        matched_password = []

        for target in targets:
            for selector in in_meeting_selectors:
                if await target.locator(selector).count() > 0:
                    matched_in_meeting.append(selector)
            for selector in lobby_selectors:
                if await target.locator(selector).count() > 0:
                    matched_lobby.append(selector)
            for selector in ended_selectors:
                if await target.locator(selector).count() > 0:
                    matched_ended.append(selector)
            for selector in error_selectors:
                if await target.locator(selector).count() > 0:
                    matched_error.append(selector)
            for selector in prejoin_selectors:
                if await target.locator(selector).count() > 0:
                    matched_prejoin.append(selector)
            for selector in joining_selectors:
                if await target.locator(selector).count() > 0:
                    matched_joining.append(selector)
            for selector in password_selectors:
                if await target.locator(selector).count() > 0:
                    matched_password.append(selector)

        if "in meeting" in title_lower or "在會議中" in page_title:
            matched_in_meeting.append("title:in_meeting")
        if "in lobby" in title_lower or "lobby" in title_lower or "大廳" in page_title:
            matched_lobby.append("title:lobby")

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

        if matched_joining:
            return MeetingStateSnapshot(
                state=MeetingState.JOINING,
                reason="Detected Webex connecting UI",
                confidence=0.8,
                evidence={"matched_selectors": matched_joining, "title": page_title, "url": page.url},
            )

        if matched_in_meeting:
            return MeetingStateSnapshot(
                state=MeetingState.IN_MEETING,
                reason="Detected Webex in-meeting UI",
                evidence={"matched_selectors": matched_in_meeting, "title": page_title, "url": page.url},
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

    async def wait_until_joined(
        self,
        page: Page,
        timeout_sec: int = 60,
        password: str | None = None,
        probe_callback: Callable[[MeetingStateSnapshot], None] | None = None,
    ) -> JoinResult:
        """Wait for Webex to enter the meeting, re-clicking join if Webex returns to prejoin."""
        logger.info(f"Waiting to join Webex meeting (timeout={timeout_sec}s)")
        start_time = time.monotonic()
        password_attempted = False
        prejoin_click_attempts = 0
        last_prejoin_click_at = 0.0

        while (time.monotonic() - start_time) < timeout_sec:
            snapshot = await self.probe_state(page)
            if probe_callback:
                probe_callback(snapshot)

            if snapshot.state == MeetingState.IN_MEETING:
                return JoinResult(success=True)

            if snapshot.state == MeetingState.LOBBY:
                return JoinResult(success=False, in_lobby=True)

            should_try_password = (
                password
                and not password_attempted
                and (snapshot.error_code == "PASSWORD_REQUIRED" or bool(snapshot.evidence.get("password_prompt")))
            )
            if should_try_password and await self.apply_password(page, password):
                password_attempted = True
                await self.short_ui_settle(fallback_sec=2.0, reason="Webex password submit")
                continue

            if snapshot.state in {MeetingState.ENDED, MeetingState.ERROR}:
                return JoinResult(
                    success=False,
                    error_code=snapshot.error_code or "MEETING_ERROR",
                    error_message=snapshot.error_message or snapshot.reason,
                )

            now = time.monotonic()
            if (
                snapshot.state == MeetingState.PREJOIN
                and prejoin_click_attempts < 3
                and now - last_prejoin_click_at >= 3.0
            ):
                try:
                    logger.info("Webex returned to prejoin while waiting; clicking join again")
                    await self.click_join(page)
                    prejoin_click_attempts += 1
                    last_prejoin_click_at = now
                    await self.short_ui_settle(fallback_sec=1.0, reason="Webex prejoin rejoin")
                    continue
                except Exception as exc:
                    logger.debug("Could not re-click Webex join from prejoin state: %s", exc)

            await self.short_ui_settle(fallback_sec=1.0, reason="Webex join wait")

        return JoinResult(
            success=False,
            error_code="JOIN_TIMEOUT",
            error_message=f"Timeout after {timeout_sec} seconds",
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
            targets = await self._webex_client_targets(page)

            # Look for layout/view options
            for target in targets:
                layout_btn = target.locator('[data-test="layout-button"], button[aria-label*="layout" i]')
                if await layout_btn.count() <= 0:
                    continue
                await layout_btn.first.click()

                if preset == "speaker":
                    option_selector = ':text("Speaker view"), :text("發言人檢視")'
                else:
                    option_selector = ':text("Grid view"), :text("格狀檢視")'

                option_match = await self.wait_for_any_selector(
                    target,
                    [option_selector],
                    state="attached",
                    timeout_ms=1000,
                    fallback_sec=0.2,
                    reason="Webex layout option",
                )
                if option_match:
                    speaker_option = self._first_locator(option_match.target.locator(option_match.selector))
                    await speaker_option.click()
                    logger.info(f"Layout set to {preset}")
                    return True

            return True
        except Exception as e:
            logger.warning(f"Could not set Webex layout: {e}")
            return False
