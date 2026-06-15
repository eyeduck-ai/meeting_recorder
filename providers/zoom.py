"""Zoom Meeting Provider for Guest Join."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.async_api import Page

from providers.base import BaseProvider, JoinResult, MeetingState, MeetingStateSnapshot

logger = logging.getLogger(__name__)


class ZoomJoinStage(StrEnum):
    """Zoom-specific page stage used to decide the next join action."""

    COOKIE_BLOCKED = "cookie_blocked"
    APP_LAUNCH_PAGE = "app_launch_page"
    BROWSER_JOIN_AVAILABLE = "browser_join_available"
    WEB_CLIENT_LOADING = "web_client_loading"
    NAME_FORM = "name_form"
    PASSWORD_FORM = "password_form"
    JOIN_READY = "join_ready"
    LOBBY = "lobby"
    IN_MEETING = "in_meeting"
    ENDED = "ended"
    ERROR = "error"


@dataclass(frozen=True)
class ZoomPageState:
    """Current Zoom page stage and the selectors that proved it."""

    stage: ZoomJoinStage
    matched_selectors: list[str]
    error_code: str | None = None
    error_message: str | None = None
    confidence: float = 1.0


class ZoomProvider(BaseProvider):
    """Provider for Zoom Meetings (Guest Join via Browser).

    Zoom requires special handling to bypass the desktop app download prompt
    and join via web browser. We use the `/wc/join/{meeting_id}` web client
    path when possible and keep `zc=0` as a fallback hint.
    """

    COOKIE_BLOCKING_SELECTORS = [
        "#onetrust-banner-sdk",
        ".onetrust-pc-dark-filter",
        "#onetrust-accept-btn-handler",
        ".onetrust-close-btn-handler",
    ]
    COOKIE_ACTION_SELECTORS = [
        "#onetrust-accept-btn-handler",
        ".onetrust-close-btn-handler",
        'button:has-text("Accept Cookies")',
        'button:has-text("ACCEPT COOKIES")',
        'button:has-text("Accept")',
        'button:has-text("接受")',
        'button[aria-label*="Accept" i]',
        '[id*="onetrust-accept"]',
    ]
    BROWSER_JOIN_SELECTORS = [
        'button:has-text("Join from browser")',
        'button:has-text("Join from Browser")',
        'button:has-text("從瀏覽器加入")',
        '[role="button"]:has-text("Join from browser")',
        'a:has-text("Join from Your Browser")',
        'a:has-text("從您的瀏覽器加入")',
        'a:has-text("從瀏覽器加入")',
        'a:has-text("join from your browser")',
        '[data-testid="join-from-browser-link"]',
        'a.joinWindowBtn:has-text("browser")',
        'button:has-text("瀏覽器")',
        'a[href*="wc/join"]',
    ]
    APP_LAUNCH_SELECTORS = [
        'button:has-text("Join from Zoom Workplace app")',
        'button:has-text("Join from Zoom app")',
        'button:has-text("從Zoom Workplace應用程式加入")',
        'text="Did not open Zoom Workplace app?"',
        'text="並未開啟Zoom Workplace應用程式？"',
        'text="Join meeting"',
        'text="加入會議"',
    ]
    NAME_SELECTORS = [
        "#input-for-name",
        "#inputname",
        'input[name="inputName"]',
        'input[placeholder*="name" i]',
        'input[placeholder*="名稱" i]',
        'input[aria-label*="name" i]',
        '[data-testid="input-name"]',
        'input[type="text"]',
    ]
    PASSWORD_SELECTORS = [
        "#inputpasscode",
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="password" i]',
        'input[placeholder*="密碼" i]',
        'input[placeholder*="passcode" i]',
    ]
    JOIN_READY_SELECTORS = [
        "#joinBtn",
        '[data-testid="join-btn"]',
        "button.join-btn",
        "button.preview-join-button",
        ".preview-join-button",
        'button:has-text("Join Meeting")',
        'button:has-text("加入會議")',
        'button[type="submit"]',
    ]
    LOBBY_SELECTORS = [
        'text="Please wait"',
        'text="等待"',
        'text="Waiting Room"',
        'text="等候室"',
        'text="host will let you in"',
        'text="主持人將會讓您加入"',
        '[data-testid="waiting-room"]',
    ]
    IN_MEETING_SELECTORS = [
        "#wc-footer",
        '[data-testid="meeting-controls"]',
        "#wc-container-left",
        ".meeting-app",
        '[data-testid="participants-btn"]',
    ]
    ENDED_SELECTORS = [
        'text="Meeting has ended"',
        'text="會議已結束"',
        'text="The host has ended this meeting"',
        'text="主持人已結束此會議"',
        'text="You have been removed"',
        'text="您已被移除"',
        '[data-testid="meeting-ended"]',
    ]
    ERROR_SELECTORS = [
        ('text="Invalid meeting ID"', "MEETING_NOT_FOUND", "無效的會議 ID"),
        ('text="無效的會議 ID"', "MEETING_NOT_FOUND", "無效的會議 ID"),
        ('text="This meeting has been locked"', "MEETING_LOCKED", "此會議已鎖定"),
        ('text="此會議已鎖定"', "MEETING_LOCKED", "此會議已鎖定"),
        ('text="Meeting not started"', "MEETING_NOT_STARTED", "會議尚未開始"),
        ('text="會議尚未開始"', "MEETING_NOT_STARTED", "會議尚未開始"),
    ]
    TRANSIENT_OVERLAY_TEXT_SELECTORS = [
        ':text("For a better meeting experience")',
        ':text("hardware acceleration")',
        ':text("Use graphics/hardware acceleration")',
        ':text("硬體加速")',
        ':text("硬件加速")',
        ':text("圖形/硬體加速")',
        ':text("图形/硬件加速")',
    ]
    TRANSIENT_OVERLAY_CLOSE_SELECTORS = [
        'button[aria-label*="close" i]',
        '[role="button"][aria-label*="close" i]',
        '[aria-label*="close" i]',
        'button[aria-label*="關閉" i]',
        '[role="button"][aria-label*="關閉" i]',
        'button[aria-label*="关闭" i]',
        '[role="button"][aria-label*="关闭" i]',
        'button:has-text("×")',
        'button:has-text("x")',
        '[class*="close" i]',
    ]

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
            if len(path_parts) >= 2 and path_parts[0] in {"j", "w"}:
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
        self._last_display_name = display_name
        self._last_password = password

        await self.wait_for_page_idle(page, timeout_ms=3000, fallback_sec=0.5, reason="Zoom prejoin page")
        state = await self._drive_join_flow(
            page,
            display_name=display_name,
            password=password,
            click_final_join=False,
            max_steps=8,
        )

        if state.stage not in {
            ZoomJoinStage.JOIN_READY,
            ZoomJoinStage.NAME_FORM,
            ZoomJoinStage.LOBBY,
            ZoomJoinStage.IN_MEETING,
            ZoomJoinStage.WEB_CLIENT_LOADING,
        }:
            logger.warning(f"Zoom prejoin did not reach join-ready state: {state.stage.value}")

    async def _accept_cookie_consent(self, page: Page) -> None:
        """Accept Zoom/OneTrust cookie consent if it is blocking the page."""
        if await self._click_first(page, self.COOKIE_ACTION_SELECTORS, "cookie consent"):
            await self._wait_until_hidden(page, self.COOKIE_BLOCKING_SELECTORS)

    async def _click_browser_join(self, page: Page) -> bool:
        """Click Zoom's browser-join fallback on launch pages."""
        # Look for "Join from Your Browser" link/button
        # Zoom often shows this as a fallback option
        if await self._click_first(page, self.BROWSER_JOIN_SELECTORS, "Join from Browser"):
            await self.wait_for_page_idle(
                page, timeout_ms=5000, fallback_sec=0.5, reason="Zoom browser join transition"
            )
            return True

        match = await self.wait_for_any_selector(
            self._targets(page),
            self.BROWSER_JOIN_SELECTORS,
            timeout_ms=2000,
            reason="Zoom Join from Browser button",
        )
        if match:
            locator = match.target.locator(match.selector)
            try:
                await locator.first.click(timeout=3000)
            except Exception:
                await locator.first.click(timeout=3000, force=True)
            logger.info(f"Clicked 'Join from Browser' using: {match.selector}")
            await self.wait_for_page_idle(
                page, timeout_ms=5000, fallback_sec=0.5, reason="Zoom browser join transition"
            )
            return True

        logger.info("No 'Join from Browser' button found")
        return False

    async def _has_any_selector(self, page: Page, selectors: list[str]) -> bool:
        """Return True when any selector is present on the page."""
        for target in self._targets(page):
            for selector in selectors:
                try:
                    locator = target.locator(selector)
                    if await locator.count() <= 0:
                        continue
                    first_locator = self._first_locator(locator)
                    if hasattr(first_locator, "is_visible") and not await first_locator.is_visible():
                        continue
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
        match = await self.wait_for_any_selector(
            self._targets(page),
            selectors,
            timeout_ms=10000,
            reason="Zoom display-name field",
        )
        if not match:
            return False

        try:
            name_input = match.target.locator(match.selector)
            count = await name_input.count()
            for index in range(count):
                element = name_input.nth(index)
                if not await element.is_visible():
                    continue
                await element.click(timeout=3000)
                await element.fill(display_name, timeout=3000)
                logger.info(f"Display name filled using: {match.selector}")
                return True
        except Exception:
            return False
        return False

    async def _navigate_to_web_client_if_possible(self, page: Page) -> bool:
        """Navigate launch URLs to the Zoom web client path when a direct candidate exists."""
        if "/wc/join/" in page.url:
            return False

        web_client_url = self.build_join_url(page.url)
        if web_client_url == page.url or not hasattr(page, "goto"):
            return False

        logger.info(f"Navigating directly to Zoom web client: {self._sanitize_url_for_evidence(web_client_url)}")
        await page.goto(web_client_url, wait_until="domcontentloaded")
        await self.wait_for_page_idle(page, timeout_ms=5000, fallback_sec=0.5, reason="Zoom web client")
        return True

    def _password_for_page(self, page: Page, password: str | None) -> str | None:
        """Return explicit password or a passcode embedded in the Zoom URL."""
        if password:
            return password
        parsed = urlparse(page.url)
        values = parse_qs(parsed.query).get("pwd", [])
        return values[0] if values else None

    async def _wait_until_hidden(self, page: Page, selectors: list[str]) -> None:
        """Best-effort wait for blocking overlays to disappear."""
        await self.wait_until_selectors_hidden(
            page,
            selectors,
            timeout_ms=3000,
            fallback_sec=0.2,
            reason="Zoom blocking overlays hidden",
        )

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
                await self.short_ui_settle(fallback_sec=0.2, reason="Zoom video toggle")
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
                await self.short_ui_settle(fallback_sec=0.2, reason="Zoom audio toggle")
                break

    async def click_join(self, page: Page) -> None:
        """Click the join meeting button.

        Args:
            page: Playwright page instance
        """
        logger.info("Clicking Zoom join button")
        state = await self._drive_join_flow(
            page,
            display_name=getattr(self, "_last_display_name", None),
            password=getattr(self, "_last_password", None),
            click_final_join=True,
            max_steps=8,
        )
        if state.stage not in {
            ZoomJoinStage.IN_MEETING,
            ZoomJoinStage.LOBBY,
            ZoomJoinStage.WEB_CLIENT_LOADING,
            ZoomJoinStage.JOIN_READY,
        }:
            logger.warning(f"Could not complete Zoom join click flow; final stage={state.stage.value}")

    async def apply_password(self, page: Page, password: str) -> bool:
        """Apply password when prompted.

        Args:
            page: Playwright page instance
            password: Meeting password

        Returns:
            True if password was entered successfully
        """
        logger.info("Checking for Zoom password dialog")

        password_selectors = [
            "#inputpasscode",
            'input[type="password"]',
            'input[placeholder*="password" i]',
            'input[placeholder*="passcode" i]',
            'input[placeholder*="密碼" i]',
        ]

        await self.wait_for_any_selector(
            self._targets(page),
            password_selectors,
            timeout_ms=1500,
            reason="Zoom password dialog",
        )
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
        return self._snapshot_from_zoom_state(page, await self._detect_zoom_state(page))

    async def dismiss_transient_overlays(self, page: Page) -> bool:
        """Dismiss Zoom in-meeting notices that can cover the captured content."""
        has_target_notice = await self._has_any_selector(page, self.TRANSIENT_OVERLAY_TEXT_SELECTORS)
        if not has_target_notice:
            return False

        if await self._dismiss_transient_overlay_via_dom(page):
            await self.short_ui_settle(fallback_sec=0.2, reason="Zoom transient overlay dismissed")
            return True

        if await self._click_first(page, self.TRANSIENT_OVERLAY_CLOSE_SELECTORS, "transient overlay close button"):
            await self.short_ui_settle(fallback_sec=0.2, reason="Zoom transient overlay dismissed")
            return True

        logger.info("Detected Zoom transient overlay but no close control was found")
        return False

    async def wait_until_joined(
        self,
        page: Page,
        timeout_sec: int = 60,
        password: str | None = None,
        probe_callback=None,
    ) -> JoinResult:
        """Actively drive Zoom through launch/prejoin states until joined or terminal."""
        logger.info(f"Waiting to join Zoom meeting (timeout={timeout_sec}s)")
        deadline = monotonic() + timeout_sec
        last_state: ZoomPageState | None = None
        effective_password = password or getattr(self, "_last_password", None)
        display_name = getattr(self, "_last_display_name", None)

        while monotonic() < deadline:
            state = await self._detect_zoom_state(page)
            last_state = state
            snapshot = self._snapshot_from_zoom_state(page, state)
            if probe_callback:
                probe_callback(snapshot)

            if state.stage == ZoomJoinStage.IN_MEETING:
                return JoinResult(success=True)
            if state.stage == ZoomJoinStage.LOBBY:
                return JoinResult(success=False, in_lobby=True)
            if state.stage == ZoomJoinStage.ENDED:
                return JoinResult(
                    success=False,
                    error_code=state.error_code or "MEETING_ENDED",
                    error_message=state.error_message or "Meeting ended before recording could continue",
                )
            if state.stage == ZoomJoinStage.ERROR:
                return JoinResult(
                    success=False,
                    error_code=state.error_code or "MEETING_ERROR",
                    error_message=state.error_message or "Zoom reported a meeting error",
                )

            if await self._advance_zoom_state(
                page,
                state,
                display_name=display_name,
                password=effective_password,
                click_final_join=True,
            ):
                continue

            await self.short_ui_settle(fallback_sec=min(1, max(0.1, deadline - monotonic())), reason="Zoom join poll")

        evidence = self._snapshot_from_zoom_state(page, last_state).evidence if last_state else {}
        return JoinResult(
            success=False,
            error_code="JOIN_TIMEOUT",
            error_message=f"Timeout after {timeout_sec} seconds; last Zoom stage={evidence.get('zoom_stage')}",
        )

    async def _detect_zoom_state(self, page: Page) -> ZoomPageState:
        """Classify the current Zoom page into the next actionable stage."""
        matched = await self._matched_selectors(page, self.IN_MEETING_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.IN_MEETING, matched)

        matched = await self._matched_selectors(page, self.ENDED_SELECTORS)
        if matched:
            return ZoomPageState(
                ZoomJoinStage.ENDED,
                matched,
                error_code="MEETING_ENDED",
                error_message="Meeting ended before recording could continue",
            )

        for selector, error_code, error_message in self.ERROR_SELECTORS:
            if await self._has_any_selector(page, [selector]):
                return ZoomPageState(
                    ZoomJoinStage.ERROR, [selector], error_code=error_code, error_message=error_message
                )

        matched = await self._matched_selectors(page, self.LOBBY_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.LOBBY, matched)

        matched = await self._matched_selectors(page, self.COOKIE_BLOCKING_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.COOKIE_BLOCKED, matched)

        matched = await self._matched_selectors(page, self.PASSWORD_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.PASSWORD_FORM, matched)

        matched = await self._matched_selectors(page, self.NAME_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.NAME_FORM, matched)

        matched = await self._matched_selectors(page, self.BROWSER_JOIN_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.BROWSER_JOIN_AVAILABLE, matched)

        matched = await self._matched_selectors(page, self.JOIN_READY_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.JOIN_READY, matched)

        matched = await self._matched_selectors(page, self.APP_LAUNCH_SELECTORS)
        if matched:
            return ZoomPageState(ZoomJoinStage.APP_LAUNCH_PAGE, matched)

        return ZoomPageState(ZoomJoinStage.WEB_CLIENT_LOADING, [], confidence=0.5)

    async def _dismiss_transient_overlay_via_dom(self, page: Page) -> bool:
        """Click a close control inside the notice that contains the target text."""
        evaluate = getattr(page, "evaluate", None)
        if not evaluate:
            return False

        script = """
        (texts) => {
            const needles = texts.map((text) => text.toLowerCase());
            const isVisible = (element) => {
                if (!element || !(element instanceof HTMLElement)) return false;
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== "hidden"
                    && style.display !== "none"
                    && rect.width > 0
                    && rect.height > 0;
            };
            const matchesClose = (element) => {
                const label = (element.getAttribute("aria-label") || "").toLowerCase();
                const title = (element.getAttribute("title") || "").toLowerCase();
                const className = String(element.className || "").toLowerCase();
                const text = (element.textContent || "").trim().toLowerCase();
                return label.includes("close")
                    || label.includes("關閉")
                    || label.includes("关闭")
                    || title.includes("close")
                    || className.includes("close")
                    || text === "×"
                    || text === "x";
            };
            const containers = Array.from(document.querySelectorAll(
                '[role="alert"], [class*="toast"], [class*="notification"], [class*="notice"], [class*="tip"], div'
            ));
            for (const container of containers) {
                if (!isVisible(container)) continue;
                const text = (container.innerText || container.textContent || "").toLowerCase();
                if (!needles.some((needle) => text.includes(needle))) continue;
                const controls = Array.from(container.querySelectorAll('button, [role="button"], [aria-label], [title]'));
                const close = controls.find((control) => isVisible(control) && matchesClose(control));
                if (close) {
                    close.click();
                    return true;
                }
            }
            return false;
        }
        """
        try:
            return bool(
                await evaluate(script, ["better meeting experience", "hardware acceleration", "硬體加速", "硬件加速"])
            )
        except Exception as e:
            logger.debug(f"Could not dismiss Zoom transient overlay via DOM: {e}")
            return False

    async def _matched_selectors(self, page: Page, selectors: list[str]) -> list[str]:
        """Return selectors present on the page or child frames."""
        matched = []
        for selector in selectors:
            if await self._has_any_selector(page, [selector]):
                matched.append(selector)
        return matched

    async def _drive_join_flow(
        self,
        page: Page,
        *,
        display_name: str | None,
        password: str | None,
        click_final_join: bool,
        max_steps: int,
    ) -> ZoomPageState:
        """Bounded state/action loop for Zoom launch and prejoin pages."""
        state = await self._detect_zoom_state(page)
        for _step in range(max_steps):
            state = await self._detect_zoom_state(page)
            if state.stage in {
                ZoomJoinStage.IN_MEETING,
                ZoomJoinStage.LOBBY,
                ZoomJoinStage.ENDED,
                ZoomJoinStage.ERROR,
            }:
                return state
            if state.stage == ZoomJoinStage.JOIN_READY and not click_final_join:
                return state
            advanced = await self._advance_zoom_state(
                page,
                state,
                display_name=display_name,
                password=password,
                click_final_join=click_final_join,
            )
            if not advanced:
                return state
            if state.stage == ZoomJoinStage.NAME_FORM and not click_final_join:
                return await self._detect_zoom_state(page)
            await self.short_ui_settle(fallback_sec=0.2, reason=f"Zoom state {state.stage.value}")
        return await self._detect_zoom_state(page)

    async def _advance_zoom_state(
        self,
        page: Page,
        state: ZoomPageState,
        *,
        display_name: str | None,
        password: str | None,
        click_final_join: bool,
    ) -> bool:
        """Perform the next action for an actionable Zoom stage."""
        if state.stage == ZoomJoinStage.COOKIE_BLOCKED:
            await self._accept_cookie_consent(page)
            return True

        if state.stage in {ZoomJoinStage.BROWSER_JOIN_AVAILABLE, ZoomJoinStage.APP_LAUNCH_PAGE}:
            clicked = await self._click_browser_join(page)
            return clicked or await self._navigate_to_web_client_if_possible(page)

        if state.stage == ZoomJoinStage.NAME_FORM and display_name:
            if await self._fill_display_name(page, self.NAME_SELECTORS, display_name):
                await self._disable_media(page)
                if click_final_join:
                    await self._click_first(
                        page,
                        self.JOIN_READY_SELECTORS + ['button:has-text("Join")'],
                        "join button",
                    )
                return True
            return False

        if state.stage == ZoomJoinStage.PASSWORD_FORM:
            effective_password = self._password_for_page(page, password)
            if effective_password:
                return await self.apply_password(page, effective_password)
            return False

        if state.stage == ZoomJoinStage.JOIN_READY and click_final_join:
            return await self._click_first(page, self.JOIN_READY_SELECTORS + ['button:has-text("Join")'], "join button")

        if state.stage == ZoomJoinStage.WEB_CLIENT_LOADING:
            return await self._navigate_to_web_client_if_possible(page)

        return False

    def _snapshot_from_zoom_state(self, page: Page, state: ZoomPageState | None) -> MeetingStateSnapshot:
        """Convert a Zoom stage to the normalized provider state contract."""
        if state is None:
            state = ZoomPageState(ZoomJoinStage.WEB_CLIENT_LOADING, [], confidence=0.5)

        state_map = {
            ZoomJoinStage.IN_MEETING: MeetingState.IN_MEETING,
            ZoomJoinStage.LOBBY: MeetingState.LOBBY,
            ZoomJoinStage.ENDED: MeetingState.ENDED,
            ZoomJoinStage.ERROR: MeetingState.ERROR,
            ZoomJoinStage.PASSWORD_FORM: MeetingState.ERROR,
            ZoomJoinStage.WEB_CLIENT_LOADING: MeetingState.JOINING,
        }
        meeting_state = state_map.get(state.stage, MeetingState.PREJOIN)
        evidence = {
            "matched_selectors": state.matched_selectors,
            "zoom_stage": state.stage.value,
            "url": self._sanitize_url_for_evidence(page.url),
            "url_kind": self._url_kind(page.url),
        }
        if state.stage == ZoomJoinStage.PASSWORD_FORM:
            evidence["password_prompt"] = True

        error_code = state.error_code
        error_message = state.error_message
        if state.stage == ZoomJoinStage.PASSWORD_FORM:
            error_code = "PASSWORD_REQUIRED"
            error_message = "需要密碼"

        return MeetingStateSnapshot(
            state=meeting_state,
            reason=f"Detected Zoom stage: {state.stage.value}",
            confidence=state.confidence,
            evidence=evidence,
            error_code=error_code,
            error_message=error_message,
        )

    def _sanitize_url_for_evidence(self, url: str) -> str:
        """Return a Zoom URL without query/fragment secrets for diagnostics."""
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()

    def _url_kind(self, url: str) -> str:
        """Classify Zoom URL shape without exposing query parameters."""
        path_parts = [part for part in urlparse(url).path.split("/") if part]
        if len(path_parts) >= 3 and path_parts[0] == "wc" and path_parts[1] == "join":
            return "web_client"
        if len(path_parts) >= 3 and path_parts[0] == "wc" and path_parts[2] == "join":
            return "web_client"
        if len(path_parts) >= 2 and path_parts[0] == "w":
            return "launch_w"
        if len(path_parts) >= 2 and path_parts[0] == "j":
            return "launch_j"
        return "unknown"

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

                if preset == "speaker":
                    option_selector = 'text="Speaker View", text="演講者檢視", [aria-label*="Speaker" i]'
                else:
                    option_selector = 'text="Gallery View", text="畫廊檢視", [aria-label*="Gallery" i]'

                option_match = await self.wait_for_any_selector(
                    page,
                    [option_selector],
                    state="attached",
                    timeout_ms=1000,
                    fallback_sec=0.2,
                    reason="Zoom layout option",
                )
                if option_match:
                    speaker_option = self._first_locator(option_match.target.locator(option_match.selector))
                    await speaker_option.click()
                    logger.info(f"Layout set to {preset}")
                    return True
        except Exception as e:
            logger.debug(f"Could not set layout: {e}")

        return False
