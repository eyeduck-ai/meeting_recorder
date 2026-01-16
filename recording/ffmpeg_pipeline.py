import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config.settings import get_settings

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
    encoding to MP4 (H.264 + AAC).
    """

    output_path: Path
    display: str = ":99"
    audio_source: str = "virtual_speaker.monitor"
    width: int = 1280
    height: int = 720
    framerate: int = 30

    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _start_time: datetime | None = field(default=None, init=False)
    _recording: bool = field(default=False, init=False)

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._recording and self._process is not None

    def _build_command(self) -> list[str]:
        """Build FFmpeg command line."""
        settings = get_settings()

        # Check if PulseAudio is available
        use_pulse = _check_pulseaudio_available(self.audio_source)

        if use_pulse:
            logger.info(f"Using PulseAudio source: {self.audio_source}")
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output
                # Video input (X11 grab)
                "-f",
                "x11grab",
                "-video_size",
                f"{self.width}x{self.height}",
                "-framerate",
                str(self.framerate),
                "-i",
                self.display,
                # Audio input (PulseAudio)
                "-f",
                "pulse",
                "-i",
                self.audio_source,
                # Video encoding
                "-c:v",
                "libx264",
                "-preset",
                settings.ffmpeg_preset,
                "-crf",
                str(settings.ffmpeg_crf),
                "-pix_fmt",
                "yuv420p",
                # Audio encoding
                "-c:a",
                "aac",
                "-b:a",
                settings.ffmpeg_audio_bitrate,
                # Output (MKV format for resilience)
                str(self.output_path),
            ]
        else:
            logger.warning("PulseAudio not available, recording video only with silent audio track")
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output
                # Video input (X11 grab)
                "-f",
                "x11grab",
                "-video_size",
                f"{self.width}x{self.height}",
                "-framerate",
                str(self.framerate),
                "-i",
                self.display,
                # Silent audio source (fallback when PulseAudio unavailable)
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                # Video encoding
                "-c:v",
                "libx264",
                "-preset",
                settings.ffmpeg_preset,
                "-crf",
                str(settings.ffmpeg_crf),
                "-pix_fmt",
                "yuv420p",
                # Audio encoding
                "-c:a",
                "aac",
                "-b:a",
                settings.ffmpeg_audio_bitrate,
                "-shortest",  # Stop when video ends
                # Output (MKV format for resilience)
                str(self.output_path),
            ]

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

        # Set environment for display
        env = os.environ.copy()
        env["DISPLAY"] = self.display

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )

            # Wait a moment to check if it started successfully
            await asyncio.sleep(1)

            if self._process.poll() is not None:
                # Process exited immediately - read stderr for error
                _, stderr = self._process.communicate(timeout=1)
                raise RuntimeError(f"FFmpeg failed to start: {stderr.decode()}")

            self._recording = True
            self._start_time = datetime.now()
            logger.info(f"Recording started: {self.output_path}")

        except Exception as e:
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

        end_time = datetime.now()

        try:
            # Send 'q' to FFmpeg to gracefully stop
            if self._process.stdin:
                try:
                    self._process.stdin.write(b"q")
                    self._process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

            # Wait for process to finish
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg didn't stop gracefully, terminating")
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg still running, killing")
                    self._process.kill()
                    self._process.wait()

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
