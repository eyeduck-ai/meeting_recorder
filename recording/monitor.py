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
        media_activity_probe: Any | None,
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
        self.media_activity_probe = media_activity_probe
        self.is_cancel_requested = is_cancel_requested
        self.is_finish_requested = is_finish_requested
        self.ffmpeg_stall_timeout_sec = ffmpeg_stall_timeout_sec
        self.ffmpeg_stall_grace_sec = ffmpeg_stall_grace_sec
        self.check_interval_sec = check_interval_sec
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self.dynamic_extension_stop_reason: str | None = None
        self._extension_started = False
        self._extension_idle_since: float | None = None
        self._unavailable_probe_count = 0
        self._activity_probe_primed = False

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return asyncio.get_event_loop().time()

    async def run(self) -> tuple[str, int | None]:
        """Run the monitor and close media activity probe resources."""
        try:
            return await self._run()
        finally:
            await self._close_media_activity_probe()

    async def _run(self) -> tuple[str, int | None]:
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

            if self.is_cancel_requested():
                raise asyncio.CancelledError("Job cancelled")

            await self._prime_dynamic_extension_probe(elapsed)

            if elapsed >= self.job.duration_sec:
                if await self._should_continue_dynamic_extension(now, elapsed):
                    await self._sleep(self.check_interval_sec)
                    continue
                if self.dynamic_extension_stop_reason is None:
                    self.dynamic_extension_stop_reason = "fixed_duration_reached"
                logger.info(f"Duration reached ({self.job.duration_sec}s)")
                return "completed", self.session.process_returncode()

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

    async def _prime_dynamic_extension_probe(self, elapsed: float) -> None:
        if self._activity_probe_primed:
            return
        enabled = bool(getattr(self.job, "dynamic_extension_enabled", False))
        if not enabled or self.media_activity_probe is None:
            return
        remaining = self.job.duration_sec - elapsed
        if remaining < 0 or remaining > self.check_interval_sec:
            return
        prime = getattr(self.media_activity_probe, "prime", None)
        if prime is not None:
            await prime(self.session)
        self._activity_probe_primed = True

    async def _close_media_activity_probe(self) -> None:
        if self.media_activity_probe is None:
            return
        close = getattr(self.media_activity_probe, "close", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:
            logger.debug("Media activity probe close failed: %s", exc)

    async def _should_continue_dynamic_extension(self, now: float, elapsed: float) -> bool:
        enabled = bool(getattr(self.job, "dynamic_extension_enabled", False))
        if not enabled:
            return False
        if self.media_activity_probe is None:
            self.dynamic_extension_stop_reason = "activity_probe_unavailable"
            return False

        extension_elapsed = max(0.0, elapsed - self.job.duration_sec)
        max_extension = int(getattr(self.job, "dynamic_extension_max_sec", 0) or 0)
        if max_extension > 0 and extension_elapsed >= max_extension:
            self.dynamic_extension_stop_reason = "max_extension_reached"
            logger.info("Dynamic extension max reached (%ss)", max_extension)
            return False

        if not self._extension_started:
            self._extension_started = True
            logger.info(
                "Dynamic extension started after scheduled duration " "(idle_timeout=%ss, max_extension=%ss)",
                getattr(self.job, "dynamic_extension_idle_sec", 300),
                max_extension,
            )

        state = await self.media_activity_probe.check(self.session)
        if not state.available:
            self._unavailable_probe_count += 1
            if self._unavailable_probe_count <= 1:
                logger.debug("Dynamic extension waiting for media activity probe baseline")
                return True
            self.dynamic_extension_stop_reason = "activity_probe_unavailable"
            logger.warning("Dynamic extension stopped because media probes are unavailable")
            return False

        self._unavailable_probe_count = 0
        if state.active is True:
            self._extension_idle_since = None
            logger.debug("Dynamic extension continuing: %s", state.reason)
            return True

        if self._extension_idle_since is None:
            self._extension_idle_since = now
            logger.debug("Dynamic extension idle timer started: %s", state.reason)
            return True

        idle_timeout = int(getattr(self.job, "dynamic_extension_idle_sec", 300) or 300)
        idle_elapsed = now - self._extension_idle_since
        if idle_elapsed >= idle_timeout:
            self.dynamic_extension_stop_reason = "idle_timeout"
            logger.info("Dynamic extension idle timeout reached (%ss)", idle_timeout)
            return False
        return True
