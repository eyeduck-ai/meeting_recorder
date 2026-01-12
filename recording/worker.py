import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright

from config.settings import get_settings
from database.models import ErrorCode, JobStatus
from providers import get_provider
from providers.base import BaseProvider, DiagnosticData
from recording.ffmpeg_pipeline import FFmpegPipeline, RecordingInfo
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig

logger = logging.getLogger(__name__)


@dataclass
class RecordingResult:
    """Result of a recording job."""

    job_id: str
    status: JobStatus
    recording_info: RecordingInfo | None = None
    diagnostic_data: DiagnosticData | None = None
    error_code: str | None = None
    error_message: str | None = None
    start_time: datetime | None = None
    joined_at: datetime | None = None
    recording_started_at: datetime | None = None
    end_time: datetime | None = None


@dataclass
class RecordingJob:
    """A recording job configuration."""

    job_id: str
    provider: str
    meeting_code: str
    display_name: str
    duration_sec: int
    output_dir: Path
    base_url: str | None = None
    password: str | None = None
    lobby_wait_sec: int = 900

    @classmethod
    def create(
        cls,
        provider: str,
        meeting_code: str,
        display_name: str,
        duration_sec: int,
        output_dir: Path | None = None,
        **kwargs,
    ) -> "RecordingJob":
        """Create a new recording job with generated ID."""
        settings = get_settings()
        job_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if output_dir is None:
            # Use timestamp + job_id for safe directory naming
            # meeting_code may contain URLs or special characters
            output_dir = settings.recordings_dir / f"{timestamp}_{job_id}"

        return cls(
            job_id=job_id,
            provider=provider,
            meeting_code=meeting_code,
            display_name=display_name,
            duration_sec=duration_sec,
            output_dir=output_dir,
            lobby_wait_sec=kwargs.get("lobby_wait_sec", settings.lobby_wait_sec),
            base_url=kwargs.get("base_url"),
            password=kwargs.get("password"),
        )


class RecordingWorker:
    """Recording worker that orchestrates the entire recording process.

    This class coordinates:
    - Virtual environment (Xvfb + PulseAudio)
    - Browser automation (Playwright)
    - Meeting provider (Jitsi, etc.)
    - FFmpeg recording pipeline
    """

    def __init__(self):
        self._current_job: RecordingJob | None = None
        self._status: JobStatus = JobStatus.QUEUED
        self._cancel_requested: bool = False
        self._status_callback: Callable[[str, JobStatus], None] | None = None

    @property
    def is_busy(self) -> bool:
        """Check if worker is currently processing a job."""
        return self._current_job is not None

    @property
    def current_status(self) -> JobStatus:
        """Get current job status."""
        return self._status

    def set_status_callback(self, callback: Callable[[str, JobStatus], None]) -> None:
        """Set callback for status updates."""
        self._status_callback = callback

    def _update_status(self, status: JobStatus) -> None:
        """Update status and notify callback."""
        self._status = status
        if self._status_callback and self._current_job:
            try:
                self._status_callback(self._current_job.job_id, status)
            except Exception as e:
                logger.warning(f"Status callback error: {e}")

    def request_cancel(self) -> bool:
        """Request cancellation of current job."""
        if self._current_job:
            self._cancel_requested = True
            return True
        return False

    async def record(self, job: RecordingJob) -> RecordingResult:
        """Execute a recording job.

        Args:
            job: Recording job configuration

        Returns:
            RecordingResult with outcome details
        """
        self._current_job = job
        self._cancel_requested = False
        self._update_status(JobStatus.STARTING)

        settings = get_settings()
        start_time = datetime.now()
        result = RecordingResult(
            job_id=job.job_id,
            status=JobStatus.STARTING,
            start_time=start_time,
        )

        # Ensure output directory exists
        job.output_dir.mkdir(parents=True, exist_ok=True)
        # Use job_id for safe filename (meeting_code may contain URLs or special chars)
        output_file = job.output_dir / f"recording_{job.job_id}.mp4"
        diagnostics_dir = settings.diagnostics_dir / job.job_id

        virtual_env = None
        browser: Browser | None = None
        page: Page | None = None
        ffmpeg: FFmpegPipeline | None = None
        provider: BaseProvider | None = None
        console_messages: list[dict] = []

        def capture_console(msg):
            """Capture console messages for diagnostics."""
            console_messages.append(
                {
                    "type": msg.type,
                    "text": msg.text,
                    "timestamp": datetime.now().isoformat(),
                }
            )

        try:
            # Get provider
            provider = get_provider(job.provider)
            logger.info(f"Using provider: {provider.name}")

            # Start virtual environment
            logger.info("Starting virtual environment")
            virtual_env = VirtualEnvironment(
                config=VirtualEnvironmentConfig(
                    width=settings.resolution_w,
                    height=settings.resolution_h,
                )
            )
            env_vars = await virtual_env.start()

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            # Start browser
            logger.info("Starting browser")
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=False,  # Need visible window for X11 grab
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    f"--window-size={settings.resolution_w},{settings.resolution_h}",
                    "--window-position=0,0",
                    "--autoplay-policy=no-user-gesture-required",
                    # No fake-ui - let permission dialogs appear and be rejected
                ],
                env={**env_vars},
            )

            context = await browser.new_context(
                viewport={"width": settings.resolution_w, "height": settings.resolution_h},
                # No permissions granted - let providers handle dialogs
            )
            page = await context.new_page()

            # Capture console messages
            page.on("console", capture_console)

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            # Navigate to meeting
            self._update_status(JobStatus.JOINING)
            join_url = provider.build_join_url(job.meeting_code, job.base_url)
            logger.info(f"Navigating to: {join_url}")
            await page.goto(join_url, wait_until="domcontentloaded")

            # Handle prejoin
            logger.info("Handling prejoin page")
            await provider.prejoin(page, job.display_name, job.password)

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            # Click join
            await provider.click_join(page)

            # Wait to join
            join_result = await provider.wait_until_joined(page, timeout_sec=60)

            if join_result.in_lobby:
                # Wait in lobby
                self._update_status(JobStatus.WAITING_LOBBY)
                logger.info(f"Waiting in lobby (max {job.lobby_wait_sec}s)")
                admitted = await provider.wait_in_lobby(page, job.lobby_wait_sec)

                if not admitted:
                    result.error_code = ErrorCode.LOBBY_TIMEOUT.value
                    raise RuntimeError("Lobby timeout - not admitted to meeting")

            elif not join_result.success:
                result.error_code = join_result.error_code or ErrorCode.JOIN_FAILED.value
                raise RuntimeError(f"Failed to join meeting: {join_result.error_code} - {join_result.error_message}")

            # Successfully joined - record timestamp
            result.joined_at = datetime.now()

            if self._cancel_requested:
                raise asyncio.CancelledError("Job cancelled")

            # Successfully joined - try to set layout
            logger.info("Joined meeting, setting layout")
            await provider.set_layout(page, "speaker")

            # Start recording
            self._update_status(JobStatus.RECORDING)
            result.recording_started_at = datetime.now()
            logger.info(f"Starting recording: {output_file}")

            ffmpeg = FFmpegPipeline(
                output_path=output_file,
                display=virtual_env.display,
                audio_source=virtual_env.pulse_monitor,
                width=settings.resolution_w,
                height=settings.resolution_h,
            )
            await ffmpeg.start()

            # Record for specified duration or until meeting ends
            recording_start = asyncio.get_event_loop().time()
            check_interval = 5  # Check every 5 seconds

            while True:
                elapsed = asyncio.get_event_loop().time() - recording_start

                # Check if duration reached
                if elapsed >= job.duration_sec:
                    logger.info(f"Duration reached ({job.duration_sec}s)")
                    break

                # Check for cancellation
                if self._cancel_requested:
                    raise asyncio.CancelledError("Job cancelled")

                # Check if meeting ended
                if await provider.detect_meeting_end(page):
                    logger.info("Meeting ended")
                    break

                # Log progress every minute
                if int(elapsed) % 60 == 0 and int(elapsed) > 0:
                    remaining = job.duration_sec - elapsed
                    logger.info(f"Recording in progress... {elapsed:.0f}s elapsed, {remaining:.0f}s remaining")

                await asyncio.sleep(check_interval)

            # Stop recording
            self._update_status(JobStatus.FINALIZING)
            recording_info = await ffmpeg.stop()
            ffmpeg = None

            # Success
            result.status = JobStatus.SUCCEEDED
            result.recording_info = recording_info
            result.end_time = datetime.now()
            self._update_status(JobStatus.SUCCEEDED)

            logger.info(f"Recording completed successfully: {recording_info.output_path}")

        except asyncio.CancelledError:
            result.status = JobStatus.CANCELED
            result.error_code = ErrorCode.CANCELED.value
            result.error_message = "Job was cancelled"
            result.end_time = datetime.now()
            self._update_status(JobStatus.CANCELED)
            logger.info("Recording cancelled")

        except Exception as e:
            result.status = JobStatus.FAILED
            if not result.error_code:
                result.error_code = ErrorCode.INTERNAL_ERROR.value
            result.error_message = str(e)
            result.end_time = datetime.now()
            self._update_status(JobStatus.FAILED)
            logger.error(f"Recording failed: {e}")

            # Collect diagnostics
            if page and provider:
                try:
                    diagnostic_data = await provider.collect_diagnostics(
                        page,
                        diagnostics_dir,
                        error_code=result.error_code,
                        error_message=result.error_message,
                        console_messages=console_messages,
                    )
                    result.diagnostic_data = diagnostic_data
                    logger.info(f"Diagnostics saved to: {diagnostics_dir}")
                except Exception as diag_error:
                    logger.warning(f"Failed to collect diagnostics: {diag_error}")

        finally:
            # Cleanup
            if ffmpeg and ffmpeg.is_recording:
                try:
                    await ffmpeg.stop()
                except Exception as e:
                    logger.warning(f"Error stopping FFmpeg: {e}")

            if browser:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning(f"Error closing browser: {e}")

            if virtual_env:
                try:
                    await virtual_env.stop()
                except Exception as e:
                    logger.warning(f"Error stopping virtual env: {e}")

            self._current_job = None

        return result


# Global worker instance (singleton for single concurrency)
_worker_instance: RecordingWorker | None = None


def get_worker() -> RecordingWorker:
    """Get the global worker instance."""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = RecordingWorker()
    return _worker_instance
