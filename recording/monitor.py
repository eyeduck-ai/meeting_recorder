"""Recording monitor loop for duration, stop requests, and meeting-end detection."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class RecordingMonitor:
    """Monitor an active recording until it should stop or fail."""

    def __init__(
        self,
        *,
        session: Any,
        job: Any,
        detection_orchestrator: Any | None,
        is_cancel_requested: Callable[[], bool],
        is_finish_requested: Callable[[], bool],
        ffmpeg_stall_timeout_sec: int,
        ffmpeg_stall_grace_sec: int,
        check_interval_sec: float = 5.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.session = session
        self.job = job
        self.detection_orchestrator = detection_orchestrator
        self.is_cancel_requested = is_cancel_requested
        self.is_finish_requested = is_finish_requested
        self.ffmpeg_stall_timeout_sec = ffmpeg_stall_timeout_sec
        self.ffmpeg_stall_grace_sec = ffmpeg_stall_grace_sec
        self.check_interval_sec = check_interval_sec
        self._clock = clock
        self._sleep = sleep or asyncio.sleep

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_event_loop().time()

    async def run(self) -> tuple[str, int | None]:
        """Run the monitor loop until completion, auto-detection, cancel, or failure."""
        recording_start = self._now()
        last_size = 0
        last_growth_time = recording_start

        effective_min_duration = (
            self.job.min_duration_sec if self.job.min_duration_sec is not None else self.job.duration_sec
        )
        if effective_min_duration > self.job.duration_sec:
            effective_min_duration = self.job.duration_sec
        logger.info(f"Recording with min_duration={effective_min_duration}s, max_duration={self.job.duration_sec}s")

        while True:
            now = self._now()
            elapsed = now - recording_start

            if self.is_finish_requested():
                logger.info("Finish requested, stopping recording early")
                return "completed", self.session.process_returncode()

            if self.session.process_returncode() is not None:
                raise RuntimeError(f"FFmpeg exited early (code {self.session.process_returncode()})")

            if (
                self.ffmpeg_stall_timeout_sec > 0
                and elapsed >= self.ffmpeg_stall_grace_sec
                and self.session.output_file.exists()
            ):
                try:
                    current_size = self.session.output_file.stat().st_size
                except OSError:
                    current_size = last_size

                if current_size > last_size:
                    last_size = current_size
                    last_growth_time = now
                elif (now - last_growth_time) >= self.ffmpeg_stall_timeout_sec:
                    raise RuntimeError(f"FFmpeg output stalled for {self.ffmpeg_stall_timeout_sec}s")

            if elapsed >= self.job.duration_sec:
                logger.info(f"Duration reached ({self.job.duration_sec}s)")
                return "completed", self.session.process_returncode()

            if self.is_cancel_requested():
                raise asyncio.CancelledError("Job cancelled")

            if elapsed >= effective_min_duration:
                if self.detection_orchestrator:
                    should_end, results = await self.detection_orchestrator.check_all(self.session.page)
                    if should_end:
                        triggered = [r for r in results if r.detected]
                        reasons = ", ".join(r.reason for r in triggered[:2])
                        logger.info(f"Meeting ended detected after min_duration: {reasons}")
                        return "auto_detected", self.session.process_returncode()
                elif await self.session.detect_meeting_end("monitor_recording"):
                    logger.info("Meeting ended")
                    return "auto_detected", self.session.process_returncode()
            elif int(elapsed) % 60 == 0 and int(elapsed) > 0:
                remaining_protection = effective_min_duration - elapsed
                logger.debug(f"Min duration protection: {remaining_protection:.0f}s remaining")

            if int(elapsed) % 60 == 0 and int(elapsed) > 0:
                remaining = self.job.duration_sec - elapsed
                logger.info(f"Recording in progress... {elapsed:.0f}s elapsed, {remaining:.0f}s remaining")

            await self._sleep(self.check_interval_sec)
