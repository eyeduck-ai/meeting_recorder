import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from playwright.async_api import Page

from utils.timezone import utc_now

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticData:
    """Diagnostic data collected on failure."""

    output_dir: Path | None = None
    screenshot_path: Path | None = None
    html_path: Path | None = None
    console_log_path: Path | None = None
    network_log_path: Path | None = None
    metadata_path: Path | None = None
    provider_state_log_path: Path | None = None
    runtime_path: Path | None = None
    error_message: str | None = None
    collected_at: datetime | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "screenshot_path": str(self.screenshot_path) if self.screenshot_path else None,
            "html_path": str(self.html_path) if self.html_path else None,
            "console_log_path": str(self.console_log_path) if self.console_log_path else None,
            "network_log_path": str(self.network_log_path) if self.network_log_path else None,
            "metadata_path": str(self.metadata_path) if self.metadata_path else None,
            "provider_state_log_path": str(self.provider_state_log_path) if self.provider_state_log_path else None,
            "runtime_path": str(self.runtime_path) if self.runtime_path else None,
            "error_message": self.error_message,
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
        }


@dataclass
class JoinResult:
    """Result of attempting to join a meeting."""

    success: bool
    in_lobby: bool = False
    error_code: str | None = None
    error_message: str | None = None


class MeetingState(StrEnum):
    """Normalized meeting state across providers."""

    PREJOIN = "prejoin"
    JOINING = "joining"
    LOBBY = "lobby"
    IN_MEETING = "in_meeting"
    ENDED = "ended"
    ERROR = "error"


@dataclass
class MeetingStateSnapshot:
    """Provider-specific state probe result."""

    state: MeetingState
    reason: str = ""
    confidence: float = 1.0
    evidence: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    collected_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        """Convert to a serializable dictionary."""
        return {
            "state": self.state.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "collected_at": self.collected_at.isoformat(),
        }


class BaseProvider(ABC):
    """Base class for meeting platform providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'jitsi', 'webex')."""
        pass

    @abstractmethod
    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        """Build the full URL to join a meeting."""
        pass

    @abstractmethod
    async def prejoin(
        self,
        page: Page,
        display_name: str,
        password: str | None = None,
    ) -> None:
        """Handle the prejoin page."""
        pass

    @abstractmethod
    async def click_join(self, page: Page) -> None:
        """Click the join button to enter the meeting."""
        pass

    async def apply_password(self, page: Page, password: str) -> bool:
        """Apply password when prompted after joining."""
        return False

    @abstractmethod
    async def probe_state(self, page: Page) -> MeetingStateSnapshot:
        """Probe the provider-specific meeting state."""
        pass

    async def wait_until_joined(
        self,
        page: Page,
        timeout_sec: int = 60,
        password: str | None = None,
        probe_callback: Callable[[MeetingStateSnapshot], None] | None = None,
    ) -> JoinResult:
        """Wait until successfully joined the meeting."""
        import asyncio

        logger.info(f"Waiting to join meeting (timeout={timeout_sec}s)")
        start_time = asyncio.get_event_loop().time()
        password_attempted = False

        while (asyncio.get_event_loop().time() - start_time) < timeout_sec:
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
                await asyncio.sleep(2)
                continue

            if snapshot.state in {MeetingState.ENDED, MeetingState.ERROR}:
                return JoinResult(
                    success=False,
                    error_code=snapshot.error_code or "MEETING_ERROR",
                    error_message=snapshot.error_message or snapshot.reason,
                )

            await asyncio.sleep(1)

        return JoinResult(
            success=False,
            error_code="JOIN_TIMEOUT",
            error_message=f"Timeout after {timeout_sec} seconds",
        )

    async def wait_in_lobby(
        self,
        page: Page,
        max_wait_sec: int = 900,
        probe_callback: Callable[[MeetingStateSnapshot], None] | None = None,
    ) -> bool:
        """Wait in the lobby until admitted or timeout."""
        import asyncio

        logger.info(f"Waiting in lobby (max={max_wait_sec}s)")
        start_time = asyncio.get_event_loop().time()
        check_interval = 5

        while (asyncio.get_event_loop().time() - start_time) < max_wait_sec:
            snapshot = await self.probe_state(page)
            if probe_callback:
                probe_callback(snapshot)

            if snapshot.state == MeetingState.IN_MEETING:
                return True
            if snapshot.state in {MeetingState.ENDED, MeetingState.ERROR}:
                return False

            elapsed = int(asyncio.get_event_loop().time() - start_time)
            if elapsed % 60 == 0 and elapsed > 0:
                logger.info(f"Still waiting in lobby... ({elapsed}s elapsed)")

            await asyncio.sleep(check_interval)

        logger.error(f"Lobby timeout after {max_wait_sec}s")
        return False

    @abstractmethod
    async def set_layout(self, page: Page, preset: str = "speaker") -> bool:
        """Attempt to set the meeting layout."""
        pass

    async def detect_meeting_end(
        self,
        page: Page,
        probe_callback: Callable[[MeetingStateSnapshot], None] | None = None,
    ) -> bool:
        """Check if the meeting has ended or we've been kicked."""
        snapshot = await self.probe_state(page)
        if probe_callback:
            probe_callback(snapshot)
        return snapshot.state in {MeetingState.ENDED, MeetingState.ERROR}

    async def collect_diagnostics(
        self,
        page: Page,
        output_dir: Path,
        error_code: str | None = None,
        error_message: str | None = None,
        console_messages: list[dict] | None = None,
        job_id: str | None = None,
        meeting_code: str | None = None,
    ) -> DiagnosticData:
        """Collect diagnostic data on failure."""
        output_dir.mkdir(parents=True, exist_ok=True)
        data = DiagnosticData(
            output_dir=output_dir,
            collected_at=utc_now(),
        )
        errors = []

        try:
            screenshot_path = output_dir / "screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            data.screenshot_path = screenshot_path
            logger.info(f"Screenshot saved: {screenshot_path}")
        except Exception as e:
            errors.append(f"Screenshot: {e}")
            logger.warning(f"Failed to capture screenshot: {e}")

        try:
            html_path = output_dir / "page.html"
            content = await page.content()
            html_path.write_text(content, encoding="utf-8")
            data.html_path = html_path
            logger.info(f"HTML saved: {html_path}")
        except Exception as e:
            errors.append(f"HTML: {e}")
            logger.warning(f"Failed to save HTML: {e}")

        if console_messages:
            try:
                console_log_path = output_dir / "console.log"
                log_content = "\n".join(f"[{msg.get('type', 'log')}] {msg.get('text', '')}" for msg in console_messages)
                console_log_path.write_text(log_content, encoding="utf-8")
                data.console_log_path = console_log_path
                logger.info(f"Console log saved: {console_log_path}")
            except Exception as e:
                errors.append(f"Console log: {e}")
                logger.warning(f"Failed to save console log: {e}")

        try:
            metadata_path = output_dir / "metadata.json"
            metadata = {
                "collected_at": data.collected_at.isoformat(),
                "job_id": job_id,
                "meeting_code": meeting_code,
                "url": page.url,
                "title": await page.title(),
                "viewport": page.viewport_size,
                "error_code": error_code,
                "error_message": error_message,
                "provider": self.name,
            }
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            data.metadata_path = metadata_path
            logger.info(f"Metadata saved: {metadata_path}")
        except Exception as e:
            errors.append(f"Metadata: {e}")
            logger.warning(f"Failed to save metadata: {e}")

        if errors:
            data.error_message = "; ".join(errors)

        return data
