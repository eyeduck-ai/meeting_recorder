import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class VirtualEnvironmentConfig:
    """Configuration for virtual display and audio."""

    width: int = 1280
    height: int = 720
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
    _started: bool = field(default=False, init=False)

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
        """Return environment variables for subprocess."""
        return {
            "DISPLAY": self.display,
            "PULSE_SERVER": "unix:/var/run/pulse/native",
        }

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

        # Kill any existing Xvfb on this display
        lock_file = f"/tmp/.X{self.config.display_num}-lock"
        if os.path.exists(lock_file):
            logger.warning(f"Removing stale lock file: {lock_file}")
            try:
                os.remove(lock_file)
            except OSError:
                pass

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

        # Wait a bit for Xvfb to start
        await asyncio.sleep(1)

        # Verify it's running
        if self._xvfb_process.poll() is not None:
            raise RuntimeError(f"Xvfb failed to start (exit code: {self._xvfb_process.returncode})")

        logger.info(f"Xvfb started on display {self.display}")

    async def _setup_pulse_audio(self) -> None:
        """Set up PulseAudio virtual audio sink.

        Note: PulseAudio should already be running from the Docker entrypoint.
        This method ensures the virtual sink is configured correctly.
        """
        try:
            # Check if PulseAudio is running
            result = subprocess.run(
                ["pactl", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                logger.warning("PulseAudio not running, audio may not work")
                return

            # Check if virtual sink exists
            result = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if self.config.pulse_sink_name not in result.stdout:
                # Create virtual sink
                logger.info(f"Creating virtual audio sink: {self.config.pulse_sink_name}")
                subprocess.run(
                    [
                        "pactl",
                        "load-module",
                        "module-null-sink",
                        f"sink_name={self.config.pulse_sink_name}",
                        f"sink_properties=device.description={self.config.pulse_sink_name}",
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

            logger.info("PulseAudio virtual sink configured")

        except FileNotFoundError:
            logger.warning("pactl not found, PulseAudio may not be installed")
        except subprocess.TimeoutExpired:
            logger.warning("PulseAudio command timed out")
        except Exception as e:
            logger.warning(f"Error setting up PulseAudio: {e}")

    async def __aenter__(self) -> "VirtualEnvironment":
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        await self.stop()
