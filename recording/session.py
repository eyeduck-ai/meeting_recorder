import json
import logging

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from config.settings import get_settings
from providers import get_provider
from providers.base import DiagnosticData, JoinResult, MeetingStateSnapshot
from recording.ffmpeg_pipeline import FFmpegPipeline, RecordingInfo
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from utils.timezone import utc_now

logger = logging.getLogger(__name__)


class RecordingSession:
    """Owns the runtime resources for a single recording attempt."""

    def __init__(self, job):
        self.job = job
        self.settings = get_settings()
        self.output_file = job.output_dir / f"recording_{job.job_id}.mkv"
        self.diagnostics_dir = self.settings.diagnostics_dir / job.job_id
        self.provider_state_path = self.diagnostics_dir / "provider_state.jsonl"
        self.runtime_path = self.diagnostics_dir / "runtime.json"

        self.virtual_env: VirtualEnvironment | None = None
        self.provider = None
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.ffmpeg: FFmpegPipeline | None = None

        self._console_messages: list[dict] = []
        self._stage_timings: dict[str, dict[str, str]] = {}
        self._current_stage: str | None = None

    @property
    def console_messages(self) -> list[dict]:
        """Return captured console messages."""
        return list(self._console_messages)

    def begin_stage(self, stage: str) -> None:
        """Mark the start of a stage."""
        self._current_stage = stage
        self._stage_timings.setdefault(stage, {})["started_at"] = utc_now().isoformat()

    def end_stage(self, stage: str, status: str = "ok") -> None:
        """Mark the end of a stage."""
        info = self._stage_timings.setdefault(stage, {})
        info["ended_at"] = utc_now().isoformat()
        info["status"] = status
        if self._current_stage == stage:
            self._current_stage = None

    def current_stage(self) -> str | None:
        """Return the stage currently in progress."""
        return self._current_stage

    def record_provider_state(self, snapshot: MeetingStateSnapshot, stage: str) -> None:
        """Append a provider state probe entry to diagnostics."""
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        entry = snapshot.to_dict()
        entry["stage"] = stage
        entry["attempt_no"] = self.job.attempt_no
        with self.provider_state_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def prepare_runtime(self) -> None:
        """Start virtual display/audio and launch the browser."""
        self.provider = get_provider(self.job.provider)
        self.job.output_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

        self.virtual_env = VirtualEnvironment(
            config=VirtualEnvironmentConfig(
                width=self.settings.resolution_w,
                height=self.settings.resolution_h,
            )
        )
        env_vars = await self.virtual_env.start()

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                f"--window-size={self.settings.resolution_w},{self.settings.resolution_h}",
                "--window-position=0,0",
                "--autoplay-policy=no-user-gesture-required",
                "--hide-scrollbars",
                "--disable-infobars",
                "--app=about:blank",
            ],
            env={**env_vars},
        )
        self.context = await self.browser.new_context(
            viewport={"width": self.settings.resolution_w, "height": self.settings.resolution_h},
            permissions=["microphone"],
        )
        self.page = await self.context.new_page()
        await self.page.add_init_script(
            """
            window.addEventListener('load', () => {
                document.body.style.overflow = 'hidden';
                document.documentElement.style.overflow = 'hidden';
            });
            """
        )
        self.page.on("console", self._capture_console)

    async def join_meeting(self) -> JoinResult:
        """Navigate to the meeting URL and wait for the first join result."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")

        join_url = self.provider.build_join_url(self.job.meeting_code, self.job.base_url)
        await self.page.goto(join_url, wait_until="domcontentloaded")

        try:
            await self.page.evaluate(
                "document.documentElement.requestFullscreen().catch(e => console.log('Fullscreen failed:', e))"
            )
        except Exception:
            pass

        await self.provider.prejoin(self.page, self.job.display_name, self.job.password)
        await self.provider.click_join(self.page)
        return await self.provider.wait_until_joined(
            self.page,
            timeout_sec=60,
            password=self.job.password,
            probe_callback=lambda snapshot: self.record_provider_state(snapshot, "join_meeting"),
        )

    async def wait_for_lobby_admission(self) -> bool:
        """Wait until admitted from the lobby."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")

        return await self.provider.wait_in_lobby(
            self.page,
            max_wait_sec=self.job.lobby_wait_sec,
            probe_callback=lambda snapshot: self.record_provider_state(snapshot, "admit_or_fail"),
        )

    async def ensure_joined(self) -> JoinResult:
        """Run a short verification check after lobby admission."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")

        return await self.provider.wait_until_joined(
            self.page,
            timeout_sec=10,
            password=self.job.password,
            probe_callback=lambda snapshot: self.record_provider_state(snapshot, "post_lobby_verify"),
        )

    async def set_layout(self, preset: str = "speaker") -> bool:
        """Ask the provider to set the meeting layout."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")
        return await self.provider.set_layout(self.page, preset)

    async def start_capture(self) -> None:
        """Start FFmpeg capture."""
        if not self.virtual_env:
            raise RuntimeError("Virtual environment not ready")

        self.ffmpeg = FFmpegPipeline(
            output_path=self.output_file,
            display=self.virtual_env.display,
            audio_source=self.virtual_env.pulse_monitor,
            width=self.settings.resolution_w,
            height=self.settings.resolution_h,
            log_path=self.diagnostics_dir / "ffmpeg.log",
        )
        await self.ffmpeg.start()

    async def finalize_capture(self) -> RecordingInfo:
        """Stop FFmpeg capture and return the resulting recording info."""
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg not started")
        recording_info = await self.ffmpeg.stop()
        self.ffmpeg = None
        return recording_info

    async def detect_meeting_end(self, stage: str) -> bool:
        """Ask the provider whether the meeting has ended."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")
        return await self.provider.detect_meeting_end(
            self.page,
            probe_callback=lambda snapshot: self.record_provider_state(snapshot, stage),
        )

    def process_returncode(self) -> int | None:
        """Return the FFmpeg process return code if FFmpeg has exited."""
        if not self.ffmpeg:
            return None
        return self.ffmpeg.process_returncode

    def build_runtime_summary(
        self,
        *,
        failure_stage: str | None = None,
        ffmpeg_exit_code: int | None = None,
        end_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        recording_info: RecordingInfo | None = None,
    ) -> dict:
        """Build and persist a runtime summary."""
        summary = {
            "job_id": self.job.job_id,
            "attempt_no": self.job.attempt_no,
            "provider": self.job.provider,
            "meeting_code": self.job.meeting_code,
            "display_name": self.job.display_name,
            "failure_stage": failure_stage,
            "ffmpeg_exit_code": ffmpeg_exit_code,
            "end_reason": end_reason,
            "error_code": error_code,
            "error_message": error_message,
            "display": self.virtual_env.display if self.virtual_env else None,
            "audio_source": self.virtual_env.pulse_monitor if self.virtual_env else None,
            "output_file": str(self.output_file),
            "provider_state_log": str(self.provider_state_path) if self.provider_state_path.exists() else None,
            "stages": self._stage_timings,
            "updated_at": utc_now().isoformat(),
        }
        if recording_info:
            summary["recording_info"] = {
                "output_path": str(recording_info.output_path),
                "file_size": recording_info.file_size,
                "duration_sec": recording_info.duration_sec,
                "start_time": recording_info.start_time.isoformat(),
                "end_time": recording_info.end_time.isoformat(),
            }
        self.runtime_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return summary

    async def collect_diagnostics(
        self,
        *,
        error_code: str | None,
        error_message: str | None,
        runtime_summary: dict,
    ) -> DiagnosticData:
        """Collect diagnostics for the current session."""
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

        if self.page and self.provider:
            diagnostic_data = await self.provider.collect_diagnostics(
                self.page,
                self.diagnostics_dir,
                error_code=error_code,
                error_message=error_message,
                console_messages=self.console_messages,
                job_id=self.job.job_id,
                meeting_code=self.job.meeting_code,
            )
        else:
            metadata_path = self.diagnostics_dir / "metadata.json"
            metadata = {
                "collected_at": utc_now().isoformat(),
                "job_id": self.job.job_id,
                "meeting_code": self.job.meeting_code,
                "provider": self.job.provider,
                "error_code": error_code,
                "error_message": error_message,
                "stage": self.current_stage() or runtime_summary.get("failure_stage"),
            }
            metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
            diagnostic_data = DiagnosticData(
                output_dir=self.diagnostics_dir,
                metadata_path=metadata_path,
                collected_at=utc_now(),
            )

        diagnostic_data.provider_state_log_path = (
            self.provider_state_path if self.provider_state_path.exists() else None
        )
        diagnostic_data.runtime_path = self.runtime_path if self.runtime_path.exists() else None
        return diagnostic_data

    async def cleanup(self) -> None:
        """Release runtime resources."""
        if self.ffmpeg and self.ffmpeg.is_recording:
            try:
                await self.ffmpeg.stop()
            except Exception as e:
                logger.warning(f"Error stopping FFmpeg: {e}")
            finally:
                self.ffmpeg = None

        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                logger.warning(f"Error closing browser context: {e}")
            finally:
                self.context = None

        if self.browser:
            try:
                await self.browser.close()
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
            finally:
                self.browser = None

        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.warning(f"Error stopping Playwright: {e}")
            finally:
                self.playwright = None

        if self.virtual_env:
            try:
                await self.virtual_env.stop()
            except Exception as e:
                logger.warning(f"Error stopping virtual env: {e}")
            finally:
                self.virtual_env = None

    def _capture_console(self, msg) -> None:
        """Capture browser console messages for diagnostics."""
        self._console_messages.append(
            {
                "type": msg.type,
                "text": msg.text,
                "timestamp": utc_now().isoformat(),
            }
        )
