import asyncio
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config.settings import get_settings
from utils.timezone import utc_now

logger = logging.getLogger(__name__)


def _check_pulseaudio_available(audio_source: str) -> bool:
    """Check if PulseAudio is available and the audio source exists."""
    try:
        result = subprocess.run(
            ["pactl", "info"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False

        # Check if the specific source exists
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True,
            timeout=5,
        )
        return audio_source in result.stdout.decode()
    except Exception as e:
        logger.debug(f"PulseAudio check failed: {e}")
        return False


@dataclass
class RecordingInfo:
    """Information about a completed recording."""

    output_path: Path
    file_size: int
    duration_sec: float
    start_time: datetime
    end_time: datetime


@dataclass
class FFmpegPipeline:
    """FFmpeg recording pipeline for capturing screen and audio.

    Captures video from X11 display and audio from PulseAudio,
    encoding based on output extension (MKV for recording stability).
    """

    output_path: Path
    display: str = ":99"
    audio_source: str = "virtual_speaker.monitor"
    width: int = 1280
    height: int = 720
    framerate: int = 30
    log_path: Path | None = None

    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _stderr_file: object | None = field(default=None, init=False, repr=False)
    _start_time: datetime | None = field(default=None, init=False)
    _recording: bool = field(default=False, init=False)

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._recording and self._process is not None

    @property
    def process_returncode(self) -> int | None:
        """Return FFmpeg process return code if exited."""
        if not self._process:
            return None
        return self._process.poll()

    def _build_command(self) -> list[str]:
        """Build FFmpeg command line."""
        settings = get_settings()

        # Check if PulseAudio is available
        use_pulse = _check_pulseaudio_available(self.audio_source)

        cmd = ["ffmpeg", "-y"]

        if settings.ffmpeg_debug_ts:
            cmd += ["-loglevel", "debug", "-debug_ts"]

        # x11grab video input with thread queue to prevent blocking
        cmd += [
            "-thread_queue_size",
            str(settings.ffmpeg_thread_queue_size),
            "-f",
            "x11grab",
            "-draw_mouse",
            "0",
            "-video_size",
            f"{self.width}x{self.height}",
            "-framerate",
            str(self.framerate),
            "-i",
            self.display,
        ]

        if use_pulse:
            logger.info(f"Using PulseAudio source: {self.audio_source}")
            # Audio input with thread queue - critical to prevent blocking
            cmd += [
                "-thread_queue_size",
                str(settings.ffmpeg_thread_queue_size),
                "-f",
                "pulse",
                "-ac",
                "2",
                "-i",
                self.audio_source,
            ]
        else:
            logger.warning("PulseAudio not available, recording video only with silent audio track")
            cmd += [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
            ]

        # Video encoding with constant frame rate sync
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            settings.ffmpeg_preset,
            "-crf",
            str(settings.ffmpeg_crf),
            "-g",
            "60",
            "-pix_fmt",
            "yuv420p",
            "-vsync",
            "cfr",  # Force constant frame rate - critical for stability
        ]

        # Audio encoding with strong timestamp correction
        # async=1000 aggressively corrects timestamp issues from PipeWire
        cmd += [
            "-af",
            settings.ffmpeg_audio_filter,
            "-c:a",
            "aac",
            "-b:a",
            settings.ffmpeg_audio_bitrate,
        ]

        if not use_pulse:
            cmd.append("-shortest")
        if self.output_path.suffix.lower() == ".mp4":
            cmd += [
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
            ]
        cmd.append(str(self.output_path))

        return cmd

    async def start(self) -> None:
        """Start recording.

        Raises:
            RuntimeError: If already recording or FFmpeg fails to start
        """
        if self._recording:
            raise RuntimeError("Already recording")

        # Ensure output directory exists
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command()
        logger.info(f"Starting FFmpeg: {' '.join(cmd)}")

        # Set environment for display and audio
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        # Ensure PipeWire-Pulse socket path is set correctly
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/0")
        env["PULSE_SERVER"] = f"unix:{xdg_runtime}/pulse/native"
        env["XDG_RUNTIME_DIR"] = xdg_runtime

        # Default: discard stdout, capture stderr to file for debugging
        # CRITICAL: Do NOT use subprocess.PIPE without reading - buffer fills up and blocks FFmpeg!
        stdout_target = subprocess.DEVNULL
        stderr_target = subprocess.DEVNULL

        if self.log_path:
            # Always log FFmpeg output to file for debugging
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_file = self.log_path.open("w", encoding="utf-8")
            stderr_target = self._stderr_file
            logger.info(f"FFmpeg logging to: {self.log_path}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=stdout_target,
                stderr=stderr_target,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )

            # Wait a moment to check if it started successfully
            await asyncio.sleep(1)

            if self._process.poll() is not None:
                # Process exited immediately - read stderr from log file for error
                stderr_output = ""
                if self._stderr_file and self.log_path:
                    try:
                        self._stderr_file.flush()
                        self._stderr_file.close()
                    finally:
                        self._stderr_file = None
                    if self.log_path.exists():
                        stderr_output = self.log_path.read_text(encoding="utf-8", errors="ignore")
                raise RuntimeError(f"FFmpeg failed to start: {stderr_output}")

            self._recording = True
            self._start_time = utc_now()
            logger.info(f"Recording started: {self.output_path}")

        except Exception as e:
            if self._stderr_file:
                try:
                    self._stderr_file.close()
                except Exception:
                    pass
                self._stderr_file = None
            self._process = None
            self._recording = False
            raise RuntimeError(f"Failed to start FFmpeg: {e}") from e

    async def stop(self) -> RecordingInfo:
        """Stop recording and return recording info.

        Returns:
            RecordingInfo with file details

        Raises:
            RuntimeError: If not recording
        """
        if not self._recording or self._process is None:
            raise RuntimeError("Not recording")

        logger.info("Stopping FFmpeg recording")

        end_time = utc_now()
        settings = get_settings()

        try:
            # Step 1: Send 'q' to FFmpeg to gracefully stop
            if self._process.stdin:
                try:
                    self._process.stdin.write(b"q")
                    self._process.stdin.flush()
                    logger.info("Sent 'q' to FFmpeg for graceful stop")
                except (BrokenPipeError, OSError):
                    logger.warning("Could not send 'q' to FFmpeg (pipe broken)")

            # Step 2: Wait 3 seconds for graceful stop
            try:
                self._process.wait(timeout=max(0, settings.ffmpeg_stop_grace_sec))
            except subprocess.TimeoutExpired:
                # Step 3: Send SIGINT (Ctrl+C) - FFmpeg handles this gracefully
                logger.info("Sending SIGINT to FFmpeg")
                try:
                    self._process.send_signal(signal.SIGINT)
                except OSError:
                    pass

                try:
                    self._process.wait(timeout=max(0, settings.ffmpeg_sigint_timeout_sec))
                except subprocess.TimeoutExpired:
                    # Step 4: SIGTERM
                    logger.warning("FFmpeg didn't respond to SIGINT, terminating")
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=max(0, settings.ffmpeg_sigterm_timeout_sec))
                    except subprocess.TimeoutExpired:
                        # Step 5: SIGKILL (last resort)
                        logger.warning("FFmpeg still running, killing")
                        self._process.kill()
                        self._process.wait()

            # Collect stderr for logging
            if self._stderr_file:
                try:
                    self._stderr_file.flush()
                    self._stderr_file.close()
                    logger.info(f"FFmpeg log saved to: {self.log_path}")
                except Exception as e:
                    logger.warning(f"Could not save FFmpeg log: {e}")
                finally:
                    self._stderr_file = None
            elif self._process.stderr:
                try:
                    self._stderr_output = self._process.stderr.read().decode(errors="ignore")
                    if self._stderr_output and self.log_path:
                        self.log_path.parent.mkdir(parents=True, exist_ok=True)
                        self.log_path.write_text(self._stderr_output, encoding="utf-8")
                        logger.info(f"FFmpeg log saved to: {self.log_path}")
                except Exception as e:
                    logger.warning(f"Could not save FFmpeg log: {e}")

            logger.info("FFmpeg stopped")

        finally:
            self._recording = False
            self._process = None

        # Get file info
        if not self.output_path.exists():
            raise RuntimeError(f"Output file not created: {self.output_path}")

        file_size = self.output_path.stat().st_size
        duration_sec = (end_time - self._start_time).total_seconds() if self._start_time else 0

        # Try to get actual duration from file
        actual_duration = await self._get_file_duration()
        if actual_duration:
            duration_sec = actual_duration

        info = RecordingInfo(
            output_path=self.output_path,
            file_size=file_size,
            duration_sec=duration_sec,
            start_time=self._start_time or end_time,
            end_time=end_time,
        )

        logger.info(
            f"Recording complete: {info.output_path} ({info.file_size / 1024 / 1024:.1f} MB, {info.duration_sec:.1f}s)"
        )

        return info

    async def _get_file_duration(self) -> float | None:
        """Get actual video duration using ffprobe."""
        try:
            result = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(self.output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            duration_str = stdout.decode().strip()
            return float(duration_str) if duration_str else None
        except Exception as e:
            logger.warning(f"Could not get file duration: {e}")
            return None

    async def __aenter__(self) -> "FFmpegPipeline":
        """Context manager entry - start recording."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - stop recording."""
        if self._recording:
            try:
                await self.stop()
            except Exception as e:
                logger.error(f"Error stopping recording: {e}")
