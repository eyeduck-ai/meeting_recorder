import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

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


class BaseProvider(ABC):
    """Base class for meeting platform providers.

    Providers implement platform-specific logic for joining meetings,
    detecting states, and collecting diagnostics.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'jitsi', 'webex')."""
        pass

    @abstractmethod
    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        """Build the full URL to join a meeting.

        Args:
            meeting_code: The meeting room code or identifier
            base_url: Optional base URL override

        Returns:
            Full URL to join the meeting
        """
        pass

    @abstractmethod
    async def prejoin(
        self,
        page: Page,
        display_name: str,
        password: str | None = None,
    ) -> None:
        """Handle the prejoin page.

        Fill in display name, disable mic/camera, handle any password prompts.

        Args:
            page: Playwright page instance
            display_name: Name to display in the meeting
            password: Optional meeting password
        """
        pass

    @abstractmethod
    async def click_join(self, page: Page) -> None:
        """Click the join button to enter the meeting.

        Args:
            page: Playwright page instance
        """
        pass

    async def apply_password(self, page: Page, password: str) -> bool:
        """Apply password when prompted after joining.

        Override this in subclasses if the provider supports password dialogs.

        Args:
            page: Playwright page instance
            password: Meeting password

        Returns:
            True if password was applied successfully
        """
        return False

    @abstractmethod
    async def wait_until_joined(self, page: Page, timeout_sec: int = 60, password: str | None = None) -> JoinResult:
        """Wait until successfully joined the meeting.

        Args:
            page: Playwright page instance
            timeout_sec: Maximum time to wait
            password: Optional password to apply if prompted

        Returns:
            JoinResult indicating success/failure and lobby status
        """
        pass

    @abstractmethod
    async def wait_in_lobby(self, page: Page, max_wait_sec: int = 900) -> bool:
        """Wait in the lobby until admitted or timeout.

        Args:
            page: Playwright page instance
            max_wait_sec: Maximum time to wait in lobby (default 15 minutes)

        Returns:
            True if admitted to meeting, False if timeout
        """
        pass

    @abstractmethod
    async def set_layout(self, page: Page, preset: str = "speaker") -> bool:
        """Attempt to set the meeting layout.

        Args:
            page: Playwright page instance
            preset: Layout preset (e.g., 'speaker', 'gallery')

        Returns:
            True if layout was set successfully
        """
        pass

    @abstractmethod
    async def detect_meeting_end(self, page: Page) -> bool:
        """Check if the meeting has ended or we've been kicked.

        Args:
            page: Playwright page instance

        Returns:
            True if meeting has ended
        """
        pass

    async def collect_diagnostics(
        self,
        page: Page,
        output_dir: Path,
        error_code: str | None = None,
        error_message: str | None = None,
        console_messages: list[dict] | None = None,
    ) -> DiagnosticData:
        """Collect diagnostic data on failure.

        Args:
            page: Playwright page instance
            output_dir: Directory to save diagnostic files
            error_code: Error code that triggered diagnostics
            error_message: Error message for context
            console_messages: List of captured console messages

        Returns:
            DiagnosticData with paths to collected files
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        data = DiagnosticData(
            output_dir=output_dir,
            collected_at=datetime.now(),
        )
        errors = []

        # 1. Screenshot
        try:
            screenshot_path = output_dir / "screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            data.screenshot_path = screenshot_path
            logger.info(f"Screenshot saved: {screenshot_path}")
        except Exception as e:
            errors.append(f"Screenshot: {e}")
            logger.warning(f"Failed to capture screenshot: {e}")

        # 2. HTML content
        try:
            html_path = output_dir / "page.html"
            content = await page.content()
            html_path.write_text(content, encoding="utf-8")
            data.html_path = html_path
            logger.info(f"HTML saved: {html_path}")
        except Exception as e:
            errors.append(f"HTML: {e}")
            logger.warning(f"Failed to save HTML: {e}")

        # 3. Console logs
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

        # 4. Metadata
        try:
            metadata_path = output_dir / "metadata.json"
            metadata = {
                "collected_at": data.collected_at.isoformat(),
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
