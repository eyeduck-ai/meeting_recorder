import json
import logging
import shutil

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config.settings import get_settings
from providers import get_provider
from providers.base import DiagnosticData, JoinResult, MeetingStateSnapshot, redact_url_secrets
from recording.ffmpeg_pipeline import FFmpegPipeline, RecordingInfo
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from utils.timezone import utc_now

logger = logging.getLogger(__name__)

AUTO_CROP_RESERVE_PX = 160
AUTO_CROP_PADDING_PX = 3
APP_INITIAL_PAGE_TIMEOUT_MS = 5000
VALID_RECORDING_BROWSER_MODES = {"app", "normal"}
VALID_RECORDING_CROP_MODES = {"auto", "manual", "off"}


class RecordingSession:
    """Owns the runtime resources for a single recording attempt."""

    def __init__(self, job):
        self.job = job
        self.settings = get_settings()
        self.resolution_w = getattr(job, "resolution_w", self.settings.resolution_w)
        self.resolution_h = getattr(job, "resolution_h", self.settings.resolution_h)
        self.recording_browser_mode = self._normalize_browser_mode(
            getattr(job, "recording_browser_mode", getattr(self.settings, "recording_browser_mode", "app"))
        )
        self.resolved_browser_mode = self._normalize_browser_mode(
            getattr(job, "resolved_browser_mode", None) or self.recording_browser_mode
        )
        self.recording_crop_mode = self._normalize_crop_mode(
            getattr(job, "recording_crop_mode", getattr(self.settings, "recording_crop_mode", "off"))
        )
        self.recording_crop_top_px = getattr(
            job,
            "recording_crop_top_px",
            getattr(self.settings, "recording_crop_top_px", 0),
        )
        self.configured_crop_top_px = max(0, int(self.recording_crop_top_px))
        self.auto_crop_reserved_px = AUTO_CROP_RESERVE_PX if self.recording_crop_mode == "auto" else 0
        self.resolved_crop_top_px = self._initial_crop_top_px()
        self.auto_crop_source = self._initial_crop_source()
        self.display_h = self.resolution_h + self._display_extra_height()
        self.output_file = job.output_dir / f"recording_{job.job_id}.mkv"
        self.diagnostics_dir = getattr(job, "diagnostics_dir", None) or (self.settings.diagnostics_dir / job.job_id)
        self.provider_state_path = self.diagnostics_dir / "provider_state.jsonl"
        self.runtime_path = self.diagnostics_dir / "runtime.json"
        self.browser_profile_dir = self.diagnostics_dir / "browser-profile"

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
        self._capture_surface: dict | None = None
        self._join_url: str | None = None
        self._browser_context_type: str | None = None

    def _normalize_browser_mode(self, value: object) -> str:
        mode = str(value).strip().lower()
        if mode not in VALID_RECORDING_BROWSER_MODES:
            choices = ", ".join(sorted(VALID_RECORDING_BROWSER_MODES))
            raise ValueError(f"recording_browser_mode must be one of: {choices}")
        return mode

    def _normalize_crop_mode(self, value: object) -> str:
        mode = str(value).strip().lower()
        if mode not in VALID_RECORDING_CROP_MODES:
            choices = ", ".join(sorted(VALID_RECORDING_CROP_MODES))
            raise ValueError(f"recording_crop_mode must be one of: {choices}")
        return mode

    def _display_extra_height(self) -> int:
        if self.recording_crop_mode == "auto":
            return self.auto_crop_reserved_px
        if self.recording_crop_mode == "manual":
            return self.configured_crop_top_px
        return 0

    def _initial_crop_top_px(self) -> int:
        if self.recording_crop_mode == "manual":
            return self.configured_crop_top_px
        if self.recording_crop_mode == "off":
            return 0
        return min(self.configured_crop_top_px, self.auto_crop_reserved_px)

    def _initial_crop_source(self) -> str:
        if self.recording_crop_mode == "manual":
            return "manual_configured"
        if self.recording_crop_mode == "off":
            return "off"
        if self.configured_crop_top_px > 0:
            return "fallback_configured"
        return "auto_pending"

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
        self._join_url = self.provider.build_join_url(self.job.meeting_code, self.job.base_url)
        self.job.output_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

        self.virtual_env = VirtualEnvironment(
            config=VirtualEnvironmentConfig(
                width=self.resolution_w,
                height=self.display_h,
            )
        )
        env_vars = await self.virtual_env.start()

        self.playwright = await async_playwright().start()
        if self.resolved_browser_mode == "app":
            await self._launch_app_context(env_vars)
        else:
            await self._launch_normal_context(env_vars)

        if not self.page:
            raise RuntimeError("Browser page was not created")
        await self._prepare_page(self.page)

    def _browser_launch_args(self) -> list[str]:
        return [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            f"--window-size={self.resolution_w},{self.display_h}",
            "--window-position=0,0",
            "--autoplay-policy=no-user-gesture-required",
            "--hide-scrollbars",
            "--disable-infobars",
        ]

    async def _launch_app_context(self, env_vars: dict[str, str]) -> None:
        if not self.playwright or not self._join_url:
            raise RuntimeError("Playwright or join URL not prepared")

        if self.browser_profile_dir.exists():
            shutil.rmtree(self.browser_profile_dir, ignore_errors=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.browser_profile_dir),
            headless=False,
            args=[*self._browser_launch_args(), f"--app={self._join_url}"],
            env={**env_vars},
            viewport={"width": self.resolution_w, "height": self.resolution_h},
            permissions=["microphone"],
        )
        self._browser_context_type = "persistent_app"
        self.page = await self._wait_for_initial_app_page(self.context)
        self.browser = None

    async def _wait_for_initial_app_page(self, context: BrowserContext) -> Page:
        if context.pages:
            return context.pages[0]
        try:
            return await context.wait_for_event("page", timeout=APP_INITIAL_PAGE_TIMEOUT_MS)
        except PlaywrightTimeoutError as e:
            raise RuntimeError("App window did not create an initial page") from e

    async def _launch_normal_context(self, env_vars: dict[str, str]) -> None:
        if not self.playwright:
            raise RuntimeError("Playwright not prepared")

        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=self._browser_launch_args(),
            env={**env_vars},
        )
        self.context = await self.browser.new_context(
            viewport={"width": self.resolution_w, "height": self.resolution_h},
            permissions=["microphone"],
        )
        self.page = await self.context.new_page()
        self._browser_context_type = "normal"

    async def _prepare_page(self, page: Page) -> None:
        await page.add_init_script(
            """
            window.addEventListener('load', () => {
                document.body.style.overflow = 'hidden';
                document.documentElement.style.overflow = 'hidden';
            });
            """
        )
        page.on("console", self._capture_console)

    async def join_meeting(self) -> JoinResult:
        """Navigate to the meeting URL and wait for the first join result."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")

        join_url = self._join_url or self.provider.build_join_url(self.job.meeting_code, self.job.base_url)
        self._join_url = join_url
        if self.resolved_browser_mode != "app":
            await self.page.goto(join_url, wait_until="domcontentloaded")

        if self.resolved_browser_mode != "app":
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

    async def dismiss_provider_overlays(self, stage: str) -> bool:
        """Ask the provider to hide transient UI that could cover the recording."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")
        try:
            dismissed = await self.provider.dismiss_transient_overlays(self.page)
            if dismissed:
                await self.probe_provider_state(stage)
            return dismissed
        except Exception as e:
            logger.warning(f"Failed to dismiss provider overlays during {stage}: {e}")
            return False

    async def prepare_capture_surface(self) -> None:
        """Best-effort browser surface preparation immediately before FFmpeg capture."""
        if not self.page:
            raise RuntimeError("Runtime not prepared")

        try:
            await self.page.bring_to_front()
        except Exception as e:
            logger.debug(f"Could not bring page to front before capture: {e}")

        fullscreen_request_allowed = getattr(self, "resolved_browser_mode", "normal") != "app"
        script = """
        async () => {
            document.body.style.overflow = 'hidden';
            document.documentElement.style.overflow = 'hidden';
            window.scrollTo(0, 0);
            let fullscreenRequested = false;
            const fullscreenRequestAllowed = __FULLSCREEN_REQUEST_ALLOWED__;
            if (fullscreenRequestAllowed && !document.fullscreenElement && document.documentElement.requestFullscreen) {
                try {
                    await document.documentElement.requestFullscreen();
                    fullscreenRequested = true;
                } catch (e) {
                    fullscreenRequested = false;
                }
            }
            return {
                fullscreenElement: Boolean(document.fullscreenElement),
                fullscreenRequestAllowed,
                fullscreenRequested,
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                outerWidth: window.outerWidth,
                outerHeight: window.outerHeight,
                screenX: window.screenX,
                screenY: window.screenY,
                screenWidth: window.screen.width,
                screenHeight: window.screen.height,
                screenAvailWidth: window.screen.availWidth,
                screenAvailHeight: window.screen.availHeight,
                devicePixelRatio: window.devicePixelRatio,
                visualViewportWidth: window.visualViewport ? window.visualViewport.width : null,
                visualViewportHeight: window.visualViewport ? window.visualViewport.height : null,
            };
        }
        """.replace("__FULLSCREEN_REQUEST_ALLOWED__", str(fullscreen_request_allowed).lower())
        try:
            self._capture_surface = await self.page.evaluate(script)
            self._resolve_crop_top_px()
            logger.info(f"Capture surface prepared: {self._capture_surface}")
        except Exception as e:
            self._capture_surface = {"error": str(e)}
            self._resolve_crop_top_px()
            logger.warning(f"Could not prepare capture surface: {e}")

    def _resolve_crop_top_px(self) -> None:
        if self.recording_crop_mode == "off":
            self.resolved_crop_top_px = 0
            self.auto_crop_source = "off"
            return

        if self.recording_crop_mode == "manual":
            self.resolved_crop_top_px = self.configured_crop_top_px
            self.auto_crop_source = "manual_configured"
            return

        surface = self._capture_surface if isinstance(self._capture_surface, dict) else {}
        try:
            outer_height = int(surface["outerHeight"])
            inner_height = int(surface["innerHeight"])
        except (KeyError, TypeError, ValueError):
            self.resolved_crop_top_px = min(self.configured_crop_top_px, self.auto_crop_reserved_px)
            self.auto_crop_source = "fallback_configured"
            return

        detected = max(0, outer_height - inner_height + AUTO_CROP_PADDING_PX)
        self.resolved_crop_top_px = min(detected, self.auto_crop_reserved_px)
        self.auto_crop_source = "browser_outer_inner"

    async def start_capture(self) -> None:
        """Start FFmpeg capture."""
        if not self.virtual_env:
            raise RuntimeError("Virtual environment not ready")

        self.ffmpeg = FFmpegPipeline(
            output_path=self.output_file,
            display=self.virtual_env.display,
            audio_source=self.virtual_env.pulse_monitor,
            width=self.resolution_w,
            height=self.resolution_h,
            capture_y=self.resolved_crop_top_px,
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

    async def probe_provider_state(self, stage: str) -> None:
        """Record one provider state snapshot without using it as an end signal."""
        if not self.page or not self.provider:
            raise RuntimeError("Runtime not prepared")
        snapshot = await self.provider.probe_state(self.page)
        self.record_provider_state(snapshot, stage)

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
        trim_summary: dict | None = None,
        dynamic_extension_stop_reason: str | None = None,
    ) -> dict:
        """Build and persist a runtime summary."""
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "job_id": self.job.job_id,
            "attempt_no": self.job.attempt_no,
            "provider": self.job.provider,
            "meeting_code": redact_url_secrets(self.job.meeting_code),
            "display_name": self.job.display_name,
            "failure_stage": failure_stage,
            "ffmpeg_exit_code": ffmpeg_exit_code,
            "end_reason": end_reason,
            "dynamic_extension_stop_reason": dynamic_extension_stop_reason,
            "error_code": error_code,
            "error_message": error_message,
            "display": self.virtual_env.display if self.virtual_env else None,
            "recording_browser_mode": self.recording_browser_mode,
            "resolved_browser_mode": self.resolved_browser_mode,
            "browser_context_type": self._browser_context_type,
            "app_launch_url": (redact_url_secrets(self._join_url) if self.resolved_browser_mode == "app" else None),
            "fallback_used": bool(getattr(self.job, "browser_fallback_used", False)),
            "fallback_reason": getattr(self.job, "browser_fallback_reason", None),
            "fallback_attempts": int(getattr(self.job, "browser_fallback_attempts", 0) or 0),
            "display_size": {
                "width": self.resolution_w,
                "height": self.display_h,
            },
            "crop_mode": self.recording_crop_mode,
            "configured_crop_top_px": self.configured_crop_top_px,
            "resolved_crop_top_px": self.resolved_crop_top_px,
            "auto_crop_source": self.auto_crop_source,
            "auto_crop_reserved_px": self.auto_crop_reserved_px,
            "capture_frame": {
                "x": 0,
                "y": self.resolved_crop_top_px,
                "width": self.resolution_w,
                "height": self.resolution_h,
            },
            "browser_surface": self._capture_surface,
            "audio_source": self.virtual_env.pulse_monitor if self.virtual_env else None,
            "output_file": str(self.output_file),
            "provider_state_log": str(self.provider_state_path) if self.provider_state_path.exists() else None,
            "stages": self._stage_timings,
            "updated_at": utc_now().isoformat(),
        }
        if trim_summary:
            summary["trim"] = trim_summary
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
                meeting_code=redact_url_secrets(self.job.meeting_code),
            )
        else:
            metadata_path = self.diagnostics_dir / "metadata.json"
            metadata = {
                "collected_at": utc_now().isoformat(),
                "job_id": self.job.job_id,
                "meeting_code": redact_url_secrets(self.job.meeting_code),
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

        if self.browser_profile_dir.exists():
            try:
                shutil.rmtree(self.browser_profile_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Error removing browser profile dir: {e}")

    def _capture_console(self, msg) -> None:
        """Capture browser console messages for diagnostics."""
        self._console_messages.append(
            {
                "type": msg.type,
                "text": msg.text,
                "timestamp": utc_now().isoformat(),
            }
        )
