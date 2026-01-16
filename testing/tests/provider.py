"""Provider login test implementation."""

import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from providers.base import BaseProvider
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from testing.models import TestResult
from testing.tests.base import BaseTest


class ProviderTest(BaseTest):
    """Test that attempts to join a meeting using a provider."""

    name = "Provider Test"
    description = "Test meeting join flow with Jitsi or Webex provider"

    def __init__(
        self,
        meeting_url: str,
        provider: str = "jitsi",
        display_name: str = "Test Recorder",
        password: str | None = None,
    ) -> None:
        super().__init__()
        self.meeting_url = meeting_url
        self.provider_type = provider.lower()
        self.display_name = display_name
        self.password = password
        self._virtual_env: VirtualEnvironment | None = None
        self._browser = None
        self._playwright = None
        self._provider: BaseProvider | None = None

    async def run(self) -> TestResult:
        """Run provider test."""
        screenshots = []

        try:
            # Validate provider type
            if self.provider_type not in ("jitsi", "webex"):
                return TestResult(success=False, error=f"Unknown provider: {self.provider_type}")

            # Create provider instance
            self._provider = self._create_provider()
            self.log(f"Testing {self._provider.name} provider")
            self.log(f"Meeting URL: {self.meeting_url}")

            # Start virtual environment
            self.log("Starting virtual environment...")
            self._virtual_env = VirtualEnvironment(config=VirtualEnvironmentConfig())
            env_vars = await self._virtual_env.start()
            self.log(f"Virtual environment started (DISPLAY={env_vars.get('DISPLAY')})")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Launch browser
            self.log("Launching browser...")
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                    "--window-position=0,0",
                    "--autoplay-policy=no-user-gesture-required",
                ],
                env={**os.environ, **env_vars},
            )

            context = await self._browser.new_context(viewport={"width": 1920, "height": 1080})
            page = await context.new_page()

            # Prepare screenshot directory
            screenshot_dir = Path("./diagnostics/test_provider")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Navigate to meeting URL
            self.log("Navigating to meeting...")
            await page.goto(self.meeting_url, wait_until="domcontentloaded", timeout=30000)
            self.log(f"Page loaded: {await page.title()}")

            # Take screenshot after navigation
            ss_path = screenshot_dir / f"{timestamp}_01_navigation.png"
            await page.screenshot(path=str(ss_path))
            screenshots.append(str(ss_path))
            self.log("Screenshot: after navigation")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Handle prejoin
            self.log("Handling prejoin page...")
            try:
                await self._provider.prejoin(page, self.display_name, self.password)
                self.log("Prejoin completed")
            except Exception as e:
                self.log(f"Prejoin error: {e}", "WARNING")

            # Take screenshot after prejoin
            ss_path = screenshot_dir / f"{timestamp}_02_prejoin.png"
            await page.screenshot(path=str(ss_path))
            screenshots.append(str(ss_path))
            self.log("Screenshot: after prejoin")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Click join
            self.log("Clicking join button...")
            try:
                await self._provider.click_join(page)
                self.log("Join button clicked")
            except Exception as e:
                self.log(f"Click join error: {e}", "WARNING")

            # Take screenshot after clicking join
            ss_path = screenshot_dir / f"{timestamp}_03_after_join_click.png"
            await page.screenshot(path=str(ss_path))
            screenshots.append(str(ss_path))
            self.log("Screenshot: after join click")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Wait for join result
            self.log("Waiting for join result (30s timeout)...")
            result = await self._provider.wait_until_joined(page, timeout_sec=30, password=self.password)

            # Take final screenshot
            ss_path = screenshot_dir / f"{timestamp}_04_final.png"
            await page.screenshot(path=str(ss_path))
            screenshots.append(str(ss_path))
            self.log("Screenshot: final state")

            # Analyze result
            if result.success:
                self.log("Successfully joined meeting!", "SUCCESS")
                return TestResult(
                    success=True,
                    data={
                        "status": "joined",
                        "screenshots": screenshots,
                        "page_url": page.url,
                    },
                )
            elif result.in_lobby:
                self.log("Reached lobby - join flow works correctly", "SUCCESS")
                return TestResult(
                    success=True,
                    data={
                        "status": "in_lobby",
                        "screenshots": screenshots,
                        "page_url": page.url,
                    },
                )
            else:
                self.log(f"Join failed: {result.error_message}", "ERROR")
                return TestResult(
                    success=False,
                    error=result.error_message or "Failed to join",
                    data={
                        "status": "failed",
                        "error_code": result.error_code,
                        "screenshots": screenshots,
                    },
                )

        except Exception as e:
            self.log(f"Provider test failed: {e}", "ERROR")
            return TestResult(
                success=False,
                error=str(e),
                data={"screenshots": screenshots} if screenshots else None,
            )

    async def cleanup(self) -> None:
        """Clean up resources."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

        if self._virtual_env:
            try:
                await self._virtual_env.stop()
            except Exception:
                pass

    def _create_provider(self) -> BaseProvider:
        """Create provider instance based on type."""
        if self.provider_type == "jitsi":
            return JitsiProvider()
        elif self.provider_type == "webex":
            return WebexProvider()
        else:
            raise ValueError(f"Unknown provider: {self.provider_type}")
