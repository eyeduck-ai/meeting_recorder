import asyncio
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class VirtualEnvironmentConfig:
    """Configuration for virtual display and audio."""

    width: int = 1920
    height: int = 1080
    depth: int = 24
    display_num: int = 99
    pulse_sink_name: str = "virtual_speaker"


@dataclass
class VirtualEnvironment:
    """Manages Xvfb virtual display and PulseAudio virtual audio.

    This class handles starting/stopping the virtual display server
    and audio system needed for headless browser recording.
    """

    config: VirtualEnvironmentConfig = field(default_factory=VirtualEnvironmentConfig)
    _xvfb_process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _audio_keepalive_process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False)
    _xvfb_owned: bool = field(default=False, init=False)

    @property
    def display(self) -> str:
        """Return DISPLAY environment variable value."""
        return f":{self.config.display_num}"

    @property
    def pulse_sink(self) -> str:
        """Return PulseAudio sink name."""
        return self.config.pulse_sink_name

    @property
    def pulse_monitor(self) -> str:
        """Return PulseAudio monitor source for recording."""
        return f"{self.config.pulse_sink_name}.monitor"

    @property
    def env_vars(self) -> dict[str, str]:
        """Return environment variables for subprocess.

        Inherits current process environment and adds/overrides
        display and audio server variables.
        """
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        # PipeWire-Pulse uses XDG_RUNTIME_DIR-based socket path
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/0")
        env["PULSE_SERVER"] = f"unix:{xdg_runtime}/pulse/native"
        env["XDG_RUNTIME_DIR"] = xdg_runtime
        return env

    async def start(self) -> dict[str, str]:
        """Start virtual display and audio.

        Returns:
            Dictionary of environment variables to use
        """
        if self._started:
            logger.warning("Virtual environment already started")
            return self.env_vars

        logger.info(
            f"Starting virtual environment: "
            f"{self.config.width}x{self.config.height}@{self.config.depth} "
            f"on display {self.display}"
        )

        await self._start_xvfb()
        await self._setup_pulse_audio()

        self._started = True
        logger.info("Virtual environment started successfully")
        return self.env_vars

    async def stop(self) -> None:
        """Stop virtual display and clean up."""
        if not self._started:
            return

        logger.info("Stopping virtual environment")

        # Stop audio keepalive process
        if self._audio_keepalive_process:
            try:
                await self._terminate_owned_process(
                    self._audio_keepalive_process,
                    name="audio keepalive",
                    timeout=2,
                )
                logger.debug("Audio keepalive process stopped")
            except Exception as e:
                logger.debug(f"Error stopping audio keepalive: {e}")
            finally:
                self._audio_keepalive_process = None

        if self._xvfb_process:
            try:
                await self._terminate_owned_process(
                    self._xvfb_process,
                    name="Xvfb",
                    timeout=5,
                )
                logger.info("Xvfb stopped")
            except Exception as e:
                logger.warning(f"Error stopping Xvfb: {e}")
            finally:
                self._xvfb_process = None

        self._cleanup_display_artifacts()
        self._started = False
        self._xvfb_owned = False
        logger.info("Virtual environment stopped")

    async def _terminate_owned_process(
        self,
        process: subprocess.Popen | None,
        *,
        name: str,
        timeout: int,
    ) -> None:
        """Terminate a process group that was started by this runtime."""
        if not process or process.poll() is not None:
            return

        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return

        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return

        process.wait()

    def _is_pid_running(self, pid: int) -> bool:
        """Return whether a pid is currently alive."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _read_display_lock_pid(self) -> int | None:
        """Read the X lock file pid for this display, if present."""
        lock_file = f"/tmp/.X{self.config.display_num}-lock"
        if not os.path.exists(lock_file):
            return None

        try:
            with open(lock_file, encoding="utf-8", errors="ignore") as fh:
                raw = fh.read().strip()
        except OSError as e:
            logger.debug(f"Failed to read X lock file {lock_file}: {e}")
            return None

        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return None

        try:
            return int(digits)
        except ValueError:
            return None

    async def _terminate_stale_display_pid(self, pid: int) -> None:
        """Terminate a non-owned Xvfb pid that still holds our display."""
        if not self._is_pid_running(pid):
            return

        logger.warning(f"Stopping stale Xvfb pid {pid} for display {self.display}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        for _ in range(20):
            if not self._is_pid_running(pid):
                return
            await asyncio.sleep(0.1)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

        for _ in range(20):
            if not self._is_pid_running(pid):
                return
            await asyncio.sleep(0.1)

    def _cleanup_display_artifacts(self) -> None:
        """Remove stale lock/socket artifacts for our display."""
        stale_pid = self._read_display_lock_pid()
        if stale_pid and self._is_pid_running(stale_pid):
            return

        lock_file = f"/tmp/.X{self.config.display_num}-lock"
        socket_path = f"/tmp/.X11-unix/X{self.config.display_num}"

        if os.path.exists(lock_file):
            logger.warning(f"Removing stale lock file: {lock_file}")
            try:
                os.remove(lock_file)
            except OSError as e:
                logger.warning(f"Failed to remove lock file: {e}")

        if os.path.exists(socket_path):
            try:
                os.remove(socket_path)
            except OSError as e:
                logger.debug(f"Failed to remove socket file: {e}")

    async def _cleanup_xvfb(self) -> None:
        """Clean up only the Xvfb process and display artifacts owned by this display."""
        if self._xvfb_process and self._xvfb_process.poll() is None:
            await self._terminate_owned_process(
                self._xvfb_process,
                name="Xvfb",
                timeout=5,
            )

        stale_pid = self._read_display_lock_pid()
        if stale_pid and (not self._xvfb_process or stale_pid != self._xvfb_process.pid):
            await self._terminate_stale_display_pid(stale_pid)

        self._cleanup_display_artifacts()

    async def _start_xvfb(self) -> None:
        """Start Xvfb virtual display server with retry logic.

        Retries up to 3 times with increasing wait times.
        Captures stderr for diagnostics when startup fails.
        """
        # Check if Xvfb is available
        try:
            subprocess.run(
                ["which", "Xvfb"],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("Xvfb not found. Please install xvfb package.")

        screen = f"{self.config.width}x{self.config.height}x{self.config.depth}"
        cmd = [
            "Xvfb",
            self.display,
            "-screen",
            "0",
            screen,
            "-ac",  # Disable access control
            "+extension",
            "GLX",
            "+extension",
            "RANDR",
            "+extension",
            "RENDER",
        ]

        max_attempts = 3
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            logger.info(f"Starting fresh Xvfb on display {self.display} (attempt {attempt}/{max_attempts})")

            # Clean up before each attempt
            await self._cleanup_xvfb()

            logger.debug(f"Starting Xvfb: {' '.join(cmd)}")

            # Capture stderr via PIPE so we can read the error message on failure.
            # On success, we close stderr immediately to prevent disk issues.
            self._xvfb_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            self._xvfb_owned = True

            # Wait for Xvfb to start (increase wait on each retry)
            wait_sec = 1.0 + (attempt - 1) * 0.5
            await asyncio.sleep(wait_sec)

            # Verify it's running
            if self._xvfb_process.poll() is None:
                # Startup succeeded — close stderr to prevent disk overflow
                # during long recordings
                try:
                    self._xvfb_process.stderr.close()
                except Exception:
                    pass
                logger.info(f"Xvfb started on display {self.display}")
                return

            # Failed — read stderr directly (no probe needed)
            exit_code = self._xvfb_process.returncode
            stderr_output = ""
            try:
                stderr_output = self._xvfb_process.stderr.read().decode(errors="replace").strip()
                self._xvfb_process.stderr.close()
            except Exception:
                pass
            last_error = stderr_output or f"exit code {exit_code}"

            logger.warning(f"Xvfb attempt {attempt}/{max_attempts} failed (exit code: {exit_code}): {last_error}")

            if attempt < max_attempts:
                await asyncio.sleep(1)

        raise RuntimeError(
            f"Xvfb failed to start after {max_attempts} attempts "
            f"(exit code: {self._xvfb_process.returncode}): {last_error}"
        )

    async def _setup_pulse_audio(self) -> None:
        """Set up virtual audio sink.

        Works with both PipeWire (via pipewire-pulse) and PulseAudio.
        The audio server should already be running from the Docker entrypoint.
        This method verifies the sink exists for this job and starts keepalive.
        """
        try:
            # Check if audio server is running (works for both PipeWire and PulseAudio)
            result = subprocess.run(
                ["pactl", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                logger.warning("Audio server not running, audio may not work")
                return

            # Log which audio server is being used
            if "PipeWire" in result.stdout:
                logger.info("Using PipeWire audio server")
            else:
                logger.info("Using PulseAudio audio server")

            sink_result = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if self.config.pulse_sink_name not in sink_result.stdout:
                logger.warning(f"Virtual audio sink not ready: {self.config.pulse_sink_name}")
                return

            source_result = subprocess.run(
                ["pactl", "list", "sources", "short"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if self.pulse_monitor not in source_result.stdout:
                logger.warning(f"Virtual audio monitor source not ready: {self.pulse_monitor}")
                return

            # Start audio keepalive process to prevent PipeWire from suspending the sink
            # PipeWire suspends idle sinks which causes FFmpeg to stall
            await self._start_audio_keepalive()

            logger.info("Virtual audio sink configured")

        except FileNotFoundError:
            logger.warning("pactl not found, audio system may not be installed")
        except subprocess.TimeoutExpired:
            logger.warning("Audio command timed out")
        except Exception as e:
            logger.warning(f"Error setting up audio: {e}")

    async def _start_audio_keepalive(self) -> None:
        """Start a background process to keep the audio sink active.

        PipeWire suspends idle sinks which causes FFmpeg audio capture to stall.
        This sends silent audio to the sink to keep it in RUNNING state.
        """
        try:
            # Use ffmpeg to generate silent audio and send to the virtual sink
            # This keeps the sink active without affecting recordings
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-f",
                "pulse",
                self.config.pulse_sink_name,
            ]

            logger.debug(f"Starting audio keepalive: {' '.join(cmd)}")

            self._audio_keepalive_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )

            # Wait a moment to ensure it started
            await asyncio.sleep(0.5)

            if self._audio_keepalive_process.poll() is None:
                logger.info("Audio keepalive process started (keeps sink active)")
            else:
                logger.warning("Audio keepalive process failed to start")
                self._audio_keepalive_process = None

        except Exception as e:
            logger.warning(f"Could not start audio keepalive: {e}")
            self._audio_keepalive_process = None

    async def __aenter__(self) -> "VirtualEnvironment":
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        await self.stop()
