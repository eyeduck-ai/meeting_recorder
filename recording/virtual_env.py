import asyncio
import logging
import os
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
                self._audio_keepalive_process.terminate()
                try:
                    self._audio_keepalive_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._audio_keepalive_process.kill()
                    self._audio_keepalive_process.wait()
                logger.debug("Audio keepalive process stopped")
            except Exception as e:
                logger.debug(f"Error stopping audio keepalive: {e}")
            finally:
                self._audio_keepalive_process = None

        # Always stop Xvfb since we always start a fresh one
        if self._xvfb_process:
            try:
                self._xvfb_process.terminate()
                try:
                    self._xvfb_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._xvfb_process.kill()
                    self._xvfb_process.wait()
                logger.info("Xvfb stopped")
            except Exception as e:
                logger.warning(f"Error stopping Xvfb: {e}")
            finally:
                self._xvfb_process = None

        self._started = False
        self._xvfb_owned = False
        logger.info("Virtual environment stopped")

    async def _start_xvfb(self) -> None:
        """Start Xvfb virtual display server."""
        # Check if Xvfb is available
        try:
            subprocess.run(
                ["which", "Xvfb"],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("Xvfb not found. Please install xvfb package.")

        # Always start a fresh Xvfb for each recording to avoid stale state issues
        # Even if one is running (e.g., from DEBUG_VNC), we start our own
        # This prevents x11grab from stalling due to Xvfb instability
        logger.info(f"Starting fresh Xvfb on display {self.display}")

        # Kill any existing Xvfb on this display
        lock_file = f"/tmp/.X{self.config.display_num}-lock"
        if os.path.exists(lock_file):
            logger.warning(f"Removing stale lock file: {lock_file}")
            try:
                os.remove(lock_file)
            except OSError:
                pass

        # Try to kill existing Xvfb processes on our display
        try:
            subprocess.run(
                ["pkill", "-f", f"Xvfb {self.display}"],
                capture_output=True,
                timeout=5,
            )
            # Give it time to clean up
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"No existing Xvfb to kill: {e}")

        # Start Xvfb
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

        logger.debug(f"Starting Xvfb: {' '.join(cmd)}")

        self._xvfb_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        self._xvfb_owned = True

        # Wait a bit for Xvfb to start
        await asyncio.sleep(1)

        # Verify it's running
        if self._xvfb_process.poll() is not None:
            raise RuntimeError(f"Xvfb failed to start (exit code: {self._xvfb_process.returncode})")

        logger.info(f"Xvfb started on display {self.display}")

    async def _setup_pulse_audio(self) -> None:
        """Set up virtual audio sink.

        Works with both PipeWire (via pipewire-pulse) and PulseAudio.
        The audio server should already be running from the Docker entrypoint.
        This method ensures the virtual sink is configured correctly.
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

            # Check if virtual sink exists
            result = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if self.config.pulse_sink_name not in result.stdout:
                # Create virtual sink (works for both PipeWire and PulseAudio)
                logger.info(f"Creating virtual audio sink: {self.config.pulse_sink_name}")
                subprocess.run(
                    [
                        "pactl",
                        "load-module",
                        "module-null-sink",
                        f"sink_name={self.config.pulse_sink_name}",
                        f"sink_properties=device.description={self.config.pulse_sink_name}",
                        "rate=48000",
                        "channels=2",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=5,
                )

            # Set as default sink
            subprocess.run(
                ["pactl", "set-default-sink", self.config.pulse_sink_name],
                capture_output=True,
                timeout=5,
            )

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
