"""Recording test implementation."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from recording.ffmpeg_pipeline import FFmpegPipeline
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from testing.models import TestResult
from testing.tests.base import BaseTest


class RecordingTest(BaseTest):
    """Test that records a short video with audio."""

    name = "Recording Test"
    description = "Test FFmpeg recording with virtual display and audio"

    def __init__(self, duration_sec: int = 10) -> None:
        super().__init__()
        self.duration_sec = min(max(duration_sec, 5), 60)  # 5-60 seconds
        self._virtual_env: VirtualEnvironment | None = None
        self._browser = None
        self._playwright = None
        self._ffmpeg: FFmpegPipeline | None = None

    async def run(self) -> TestResult:
        """Run recording test."""
        output_path = None

        try:
            # Start virtual environment
            self.log("Starting virtual environment...")
            self._virtual_env = VirtualEnvironment(config=VirtualEnvironmentConfig())
            env_vars = await self._virtual_env.start()
            self.log(f"Virtual environment started (DISPLAY={env_vars.get('DISPLAY')})")

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Launch browser with test content
            self.log("Launching browser with test content...")
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

            # Load a colorful test page
            self.log("Loading test content...")
            await page.set_content(self._get_test_html())
            await asyncio.sleep(1)

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Prepare output path
            output_dir = Path("./diagnostics/test_recordings")
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = output_dir / f"recording_test_{timestamp}.mp4"

            # Start FFmpeg recording
            self.log(f"Starting FFmpeg recording ({self.duration_sec}s)...")
            self._ffmpeg = FFmpegPipeline(
                output_path=output_path,
                display=env_vars.get("DISPLAY", ":99"),
                resolution=(1920, 1080),
            )
            await self._ffmpeg.start()

            # Wait for recording duration
            for i in range(self.duration_sec):
                if self.is_cancelled:
                    break
                await asyncio.sleep(1)
                self.log(f"Recording... {i + 1}/{self.duration_sec}s")

            # Stop recording
            self.log("Stopping recording...")
            recording_info = await self._ffmpeg.stop()

            if self.is_cancelled:
                return TestResult(success=False, error="Cancelled")

            # Verify output
            if not output_path.exists():
                return TestResult(success=False, error="Output file not created")

            file_size = output_path.stat().st_size
            if file_size < 1000:
                return TestResult(success=False, error=f"Output file too small: {file_size} bytes")

            self.log(f"Recording saved: {output_path}")
            self.log(f"File size: {file_size / 1024:.1f} KB")
            if recording_info:
                self.log(f"Duration: {recording_info.duration_sec:.1f}s")

            return TestResult(
                success=True,
                data={
                    "output_path": str(output_path),
                    "file_size": file_size,
                    "duration_sec": recording_info.duration_sec if recording_info else self.duration_sec,
                },
            )

        except Exception as e:
            self.log(f"Recording test failed: {e}", "ERROR")
            return TestResult(success=False, error=str(e))

    async def cleanup(self) -> None:
        """Clean up resources."""
        if self._ffmpeg:
            try:
                await self._ffmpeg.stop()
            except Exception:
                pass

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

    def _get_test_html(self) -> str:
        """Generate colorful test HTML content."""
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {
                    margin: 0;
                    padding: 0;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    background: linear-gradient(45deg, #ff6b6b, #4ecdc4, #45b7d1, #96ceb4);
                    background-size: 400% 400%;
                    animation: gradient 3s ease infinite;
                    font-family: Arial, sans-serif;
                }
                @keyframes gradient {
                    0% { background-position: 0% 50%; }
                    50% { background-position: 100% 50%; }
                    100% { background-position: 0% 50%; }
                }
                .container {
                    text-align: center;
                    color: white;
                    text-shadow: 2px 2px 4px rgba(0,0,0,0.5);
                }
                h1 { font-size: 4em; margin: 0; }
                .time {
                    font-size: 3em;
                    font-family: monospace;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Recording Test</h1>
                <div class="time" id="time"></div>
            </div>
            <script>
                function updateTime() {
                    document.getElementById('time').textContent =
                        new Date().toLocaleTimeString();
                }
                setInterval(updateTime, 1000);
                updateTime();
            </script>
        </body>
        </html>
        """
