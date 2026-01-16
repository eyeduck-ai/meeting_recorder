"""Browser test implementation."""

import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from testing.models import TestResult
from testing.tests.base import BaseTest


class BrowserTest(BaseTest):
    """Test that launches a virtual browser and takes a screenshot."""

    name = "Browser Test"
    description = "Launch Playwright browser in virtual display and take screenshot"

    def __init__(self, test_url: str = "https://example.com") -> None:
        super().__init__()
        self.test_url = test_url
        self._virtual_env: VirtualEnvironment | None = None
        self._browser = None
        self._playwright = None

    async def run(self) -> TestResult:
        """Run browser test."""
        screenshot_path = None

        try:
            # Start virtual environment
            self.log("Starting virtual environment...")
            self._virtual_env = VirtualEnvironment(config=VirtualEnvironmentConfig())
            env_vars = await self._virtual_env.start()
            self.log(f"Virtual environment started (DISPLAY={env_vars.get('DISPLAY')})")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Launch browser
            self.log("Launching Playwright...")
            self._playwright = await async_playwright().start()

            self.log("Launching Chromium browser...")
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                    "--window-position=0,0",
                ],
                env={**os.environ, **env_vars},
            )

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Create page and navigate
            self.log("Creating browser context...")
            context = await self._browser.new_context(viewport={"width": 1920, "height": 1080})
            page = await context.new_page()

            self.log(f"Navigating to {self.test_url}...")
            await page.goto(self.test_url, wait_until="networkidle", timeout=30000)

            self.log(f"Page loaded: {await page.title()}")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Take screenshot
            self.log("Taking screenshot...")
            screenshot_dir = Path("./diagnostics/test_screenshots")
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = screenshot_dir / f"browser_test_{timestamp}.png"

            await page.screenshot(path=str(screenshot_path), full_page=False)
            self.log(f"Screenshot saved: {screenshot_path}")

            # Get page info
            viewport = page.viewport_size
            url = page.url

            return TestResult(
                success=True,
                data={
                    "screenshot_path": str(screenshot_path),
                    "page_title": await page.title(),
                    "page_url": url,
                    "viewport": viewport,
                },
            )

        except Exception as e:
            self.log(f"Browser test failed: {e}", "ERROR")
            return TestResult(success=False, error=str(e))

    async def cleanup(self) -> None:
        """Clean up browser and virtual environment."""
        if self._browser:
            try:
                self.log("Closing browser...")
                await self._browser.close()
            except Exception as e:
                self.log(f"Error closing browser: {e}", "WARNING")

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

        if self._virtual_env:
            try:
                self.log("Stopping virtual environment...")
                await self._virtual_env.stop()
            except Exception as e:
                self.log(f"Error stopping virtual env: {e}", "WARNING")
