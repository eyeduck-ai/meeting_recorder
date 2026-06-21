"""Media activity probes and trim helpers for smart recording boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sys
from array import array
from bisect import bisect_right
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from recording.ffmpeg_pipeline import RecordingInfo
from recording.subprocess_utils import run_bounded_subprocess
from utils.timezone import utc_now

logger = logging.getLogger(__name__)

VIDEO_SAMPLE_WIDTH = 96
VIDEO_SAMPLE_HEIGHT = 54
VIDEO_FRAME_SIZE = VIDEO_SAMPLE_WIDTH * VIDEO_SAMPLE_HEIGHT
AUDIO_SAMPLE_RATE = 8000


@dataclass(frozen=True)
class ActivityConfig:
    """Thresholds and sampling policy for media activity detection."""

    audio_threshold_db: float = -45.0
    video_diff_threshold: float = 0.015
    sample_interval_sec: float = 5.0
    sample_window_sec: float = 1.0
    smart_trim_pre_roll_sec: float = 2.0
    smart_trim_end_post_roll_sec: float = 5.0


@dataclass(frozen=True)
class MediaActivityState:
    """One activity probe result."""

    audio_active: bool | None
    video_active: bool | None
    audio_level_db: float | None = None
    video_diff: float | None = None
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.audio_active is not None or self.video_active is not None

    @property
    def active(self) -> bool | None:
        if self.audio_active is True or self.video_active is True:
            return True
        if self.audio_active is None and self.video_active is None:
            return None
        return False


@dataclass(frozen=True)
class ActivitySample(MediaActivityState):
    """A timestamped completed-file activity sample."""

    timestamp_sec: float = 0.0


@dataclass(frozen=True)
class TrimDecision:
    """The post-recording smart trim decision."""

    status: str
    reason: str
    trim_start_sec: float
    trim_end_sec: float | None
    duration_sec: float | None
    samples: list[ActivitySample] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def should_trim(self) -> bool:
        if self.status != "trimmed" or self.duration_sec is None or self.trim_end_sec is None:
            return False
        return self.trim_start_sec > 0.5 or self.trim_end_sec < self.duration_sec - 0.5


def build_activity_config(
    *,
    audio_threshold_db: float = -45.0,
    video_diff_threshold: float = 0.015,
    sample_interval_sec: float = 5.0,
    sample_window_sec: float = 1.0,
    smart_trim_pre_roll_sec: float = 2.0,
    smart_trim_end_post_roll_sec: float = 5.0,
) -> ActivityConfig:
    return ActivityConfig(
        audio_threshold_db=audio_threshold_db,
        video_diff_threshold=video_diff_threshold,
        sample_interval_sec=max(1.0, sample_interval_sec),
        sample_window_sec=max(0.25, sample_window_sec),
        smart_trim_pre_roll_sec=max(0.0, smart_trim_pre_roll_sec),
        smart_trim_end_post_roll_sec=max(0.0, smart_trim_end_post_roll_sec),
    )


def _byte_diff_ratio(previous: bytes, current: bytes) -> float:
    if not previous or not current:
        return 1.0
    compared = min(len(previous), len(current))
    if compared == 0:
        return 1.0
    diff = sum(abs(a - b) for a, b in zip(previous[:compared], current[:compared], strict=False))
    max_diff = 255 * compared
    size_penalty = abs(len(previous) - len(current)) / max(len(previous), len(current))
    return min(1.0, (diff / max_diff) + size_penalty)


def _image_diff_ratio(previous: bytes, current: bytes) -> float:
    try:
        from io import BytesIO

        from PIL import Image

        image1 = Image.open(BytesIO(previous)).resize((96, 54)).convert("L")
        image2 = Image.open(BytesIO(current)).resize((96, 54)).convert("L")
        pixels1 = list(image1.getdata())
        pixels2 = list(image2.getdata())
        if len(pixels1) != len(pixels2):
            return 1.0
        diff = sum(abs(a - b) for a, b in zip(pixels1, pixels2, strict=False))
        return diff / (255 * len(pixels1))
    except Exception:
        return _byte_diff_ratio(previous, current)


def _db_from_peak(max_abs_sample: int) -> float:
    if max_abs_sample <= 0:
        return -120.0
    return 20.0 * math.log10(min(max_abs_sample, 32767) / 32768.0)


def _sample_timestamps(duration: float, interval_sec: float) -> list[float]:
    timestamps: list[float] = []
    timestamp = 0.0
    while timestamp < duration:
        timestamps.append(timestamp)
        timestamp += interval_sec
    return timestamps


def _sample_timestamps_between(start_sec: float, end_sec: float, interval_sec: float) -> list[float]:
    timestamps: list[float] = []
    timestamp = max(0.0, start_sec)
    while timestamp < end_sec:
        timestamps.append(timestamp)
        timestamp += interval_sec
    return timestamps


def _activity_probe_timeout_sec(duration: float, probe_start_sec: float, probe_end_sec: float) -> float:
    span = max(0.0, probe_end_sec - probe_start_sec)
    if span > 0 and span < duration:
        return max(10.0, min(120.0, span * 4.0 + 10.0))
    return max(60.0, min(900.0, duration * 0.25))


def _apply_video_transition(
    states: list[tuple[bool | None, float | None]],
    *,
    index: int,
    diff: float,
    threshold: float,
) -> None:
    active = diff >= threshold
    previous_index = index - 1
    if active:
        if 0 <= previous_index < len(states):
            states[previous_index] = (True, diff)
        if 0 <= index < len(states):
            states[index] = (True, diff)
        return

    if 0 <= previous_index < len(states) and states[previous_index][0] is None:
        states[previous_index] = (False, diff)
    if 0 <= index < len(states) and states[index][0] is None:
        states[index] = (False, diff)


def _video_activity_from_frames(
    frames: list[bytes],
    *,
    sample_count: int,
    threshold: float,
) -> list[tuple[bool | None, float | None]]:
    """Map frame diffs to sample activity, backfilling transition starts."""
    states: list[tuple[bool | None, float | None]] = [(None, None) for _ in range(sample_count)]
    if len(frames) < 2 or sample_count == 0:
        return states

    comparable_count = min(len(frames), sample_count)
    for idx in range(1, comparable_count):
        diff = _byte_diff_ratio(frames[idx - 1], frames[idx])
        _apply_video_transition(states, index=idx, diff=diff, threshold=threshold)
    return states


async def _probe_media_duration_sec(input_path: Path) -> float | None:
    try:
        result = await run_bounded_subprocess(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
            timeout_sec=10.0,
            stdout_limit=1024,
            stderr_limit=2048,
        )
    except FileNotFoundError:
        logger.warning("Could not probe duration for %s: ffprobe not found", input_path)
        return None
    except Exception as exc:
        logger.warning("Could not probe duration for %s: %s", input_path, exc)
        return None
    if result.returncode != 0:
        logger.warning("Could not probe duration for %s: %s", input_path, result.stderr[:200])
        return None
    try:
        return float(result.stdout.decode(errors="ignore").strip())
    except ValueError:
        return None


async def _read_stderr_limited(stream: asyncio.StreamReader | None, *, limit: int = 4096) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if total < limit:
            take = chunk[: limit - total]
            chunks.append(take)
            total += len(take)
    return b"".join(chunks).decode(errors="ignore")


async def _read_stream_to_log_or_excerpt(
    stream: asyncio.StreamReader | None,
    *,
    log_path: Path | None = None,
    limit: int = 4096,
) -> str:
    """Drain a process stream while optionally writing full content to disk."""
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    log_file = None
    try:
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("wb")
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            if log_file:
                log_file.write(chunk)
            if total < limit:
                take = chunk[: limit - total]
                chunks.append(take)
                total += len(take)
    finally:
        if log_file:
            log_file.close()
    return b"".join(chunks).decode(errors="ignore")


async def _close_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _run_ffmpeg_trim(
    *cmd: str,
    timeout_sec: float,
    log_path: Path | None = None,
) -> tuple[int, str]:
    """Run trim FFmpeg without accumulating full stdout/stderr in memory."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "ffmpeg not found"

    stdout_task = asyncio.create_task(_read_stream_to_log_or_excerpt(process.stdout, limit=1024))
    stderr_task = asyncio.create_task(_read_stream_to_log_or_excerpt(process.stderr, log_path=log_path, limit=4096))
    timed_out = False
    try:
        async with asyncio.timeout(timeout_sec):
            returncode = await process.wait()
    except TimeoutError:
        timed_out = True
        await _close_process(process)
        returncode = 124

    stdout_excerpt = await stdout_task
    stderr_excerpt = await stderr_task
    message = stderr_excerpt or stdout_excerpt
    if timed_out:
        message = (message + "\ntrim timed out").strip()
    return returncode or 0, message


class _PersistentAudioMeter:
    """Maintain recent PulseAudio peak levels from one long-running FFmpeg process."""

    def __init__(
        self,
        *,
        source: str,
        config: ActivityConfig,
        process_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] = asyncio.create_subprocess_exec,
    ):
        self.source = source
        self.config = config
        self._process_factory = process_factory
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[str] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._recent_peaks: deque[tuple[float, int]] = deque()
        self._started = False
        self._error: str | None = None
        self._remainder = b""

    @property
    def error(self) -> str | None:
        return self._error

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        cmd = (
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-f",
            "pulse",
            "-i",
            self.source,
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        )
        try:
            self._process = await self._process_factory(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._error = "ffmpeg not found"
            return
        except Exception as exc:
            self._error = f"audio meter start failed: {exc}"
            return

        self._stderr_task = asyncio.create_task(_read_stderr_limited(self._process.stderr))
        self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        await _close_process(self._process)
        if self._reader_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._stderr_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

    def snapshot(self) -> tuple[bool | None, float | None]:
        if self._error:
            return None, None
        self._trim_recent_peaks(asyncio.get_running_loop().time())
        if not self._recent_peaks:
            return None, None
        peak = max(value for _, value in self._recent_peaks)
        level = _db_from_peak(peak)
        return level >= self.config.audio_threshold_db, level

    async def _read_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while True:
                chunk = await self._process.stdout.read(8192)
                if not chunk:
                    break
                self._consume_pcm_chunk(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"audio meter read failed: {exc}"
        finally:
            if self._process.returncode is None:
                returncode = await self._process.wait()
            else:
                returncode = self._process.returncode
            if returncode not in (0, None) and self._error is None:
                stderr = ""
                if self._stderr_task:
                    with contextlib.suppress(Exception):
                        stderr = await self._stderr_task
                self._error = f"audio meter exited with code {returncode}: {stderr[:200]}"

    def _consume_pcm_chunk(self, chunk: bytes) -> None:
        payload = self._remainder + chunk
        if len(payload) % 2:
            self._remainder = payload[-1:]
            payload = payload[:-1]
        else:
            self._remainder = b""
        if not payload:
            return
        pcm = array("h")
        pcm.frombytes(payload)
        if sys.byteorder != "little":
            pcm.byteswap()
        if not pcm:
            return
        peak = max(abs(value) for value in pcm)
        now = asyncio.get_running_loop().time()
        self._recent_peaks.append((now, peak))
        self._trim_recent_peaks(now)

    def _trim_recent_peaks(self, now: float) -> None:
        window = max(0.25, self.config.sample_window_sec)
        while self._recent_peaks and now - self._recent_peaks[0][0] > window:
            self._recent_peaks.popleft()


class LiveMediaActivityProbe:
    """Probe live recording activity without provider DOM selectors."""

    def __init__(self, config: ActivityConfig):
        self.config = config
        self._last_screenshot: bytes | None = None
        self._audio_meter: _PersistentAudioMeter | None = None

    async def prime(self, session: Any) -> None:
        """Warm live media probes before dynamic extension begins."""
        results = await asyncio.gather(
            self._check_audio(session),
            self._check_video(session),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.debug("Live media probe prime failed: %s", result)

    async def close(self) -> None:
        if self._audio_meter:
            await self._audio_meter.close()
            self._audio_meter = None

    async def check(self, session: Any) -> MediaActivityState:
        audio_result, video_result = await asyncio.gather(
            self._check_audio(session),
            self._check_video(session),
            return_exceptions=True,
        )
        if isinstance(audio_result, Exception):
            logger.debug("Live audio probe failed: %s", audio_result)
            audio_active, audio_level = None, None
        else:
            audio_active, audio_level = audio_result
        if isinstance(video_result, Exception):
            logger.debug("Live video probe failed: %s", video_result)
            video_active, video_diff = None, None
        else:
            video_active, video_diff = video_result
        if audio_active is None and video_active is None:
            reason = "media probes unavailable"
        elif audio_active is True or video_active is True:
            reason = "media activity detected"
        else:
            reason = "audio silent and video still"
        return MediaActivityState(
            audio_active=audio_active,
            video_active=video_active,
            audio_level_db=audio_level,
            video_diff=video_diff,
            reason=reason,
        )

    async def _check_audio(self, session: Any) -> tuple[bool | None, float | None]:
        source = getattr(getattr(session, "virtual_env", None), "pulse_monitor", None)
        if not source:
            return None, None
        if self._audio_meter is None or self._audio_meter.source != source:
            if self._audio_meter:
                await self._audio_meter.close()
            self._audio_meter = _PersistentAudioMeter(source=source, config=self.config)
            await self._audio_meter.start()
        if self._audio_meter.error:
            logger.debug("Live audio probe unavailable: %s", self._audio_meter.error)
            return None, None
        return self._audio_meter.snapshot()

    async def _check_video(self, session: Any) -> tuple[bool | None, float | None]:
        page = getattr(session, "page", None)
        if not page:
            return None, None
        try:
            current = await page.screenshot(type="jpeg", quality=35)
        except Exception as exc:
            logger.debug("Live video probe unavailable: %s", exc)
            return None, None
        if self._last_screenshot is None:
            self._last_screenshot = current
            return None, None
        diff = _image_diff_ratio(self._last_screenshot, current)
        self._last_screenshot = current
        return diff >= self.config.video_diff_threshold, diff


class RecordingActivityAnalyzer:
    """Analyze a completed recording and derive smart trim boundaries."""

    def __init__(self, config: ActivityConfig):
        self.config = config
        self._diagnostics: dict[str, Any] = {}

    async def analyze(self, input_path: Path) -> TrimDecision:
        started_at = perf_counter()
        self._diagnostics = {
            "coarse_interval_sec": self.config.sample_interval_sec,
            "sample_window_sec": self.config.sample_window_sec,
            "probes": [],
        }
        duration = await self._probe_duration_sec(input_path)
        if duration is None or duration <= 0:
            self._diagnostics["analysis_elapsed_sec"] = round(perf_counter() - started_at, 3)
            return TrimDecision(
                status="skipped",
                reason="duration unavailable",
                trim_start_sec=0.0,
                trim_end_sec=None,
                duration_sec=duration,
                diagnostics=self._diagnostics,
            )

        samples = await self._collect_samples(input_path, duration)
        self._diagnostics["coarse_sample_count"] = len(samples)
        available = [sample for sample in samples if sample.active is not None]
        if not available:
            self._diagnostics["analysis_elapsed_sec"] = round(perf_counter() - started_at, 3)
            return TrimDecision(
                status="skipped",
                reason="media probes unavailable",
                trim_start_sec=0.0,
                trim_end_sec=duration,
                duration_sec=duration,
                samples=samples,
                diagnostics=self._diagnostics,
            )

        active_samples = [sample for sample in samples if sample.active is True]
        if not active_samples:
            self._diagnostics["analysis_elapsed_sec"] = round(perf_counter() - started_at, 3)
            return TrimDecision(
                status="skipped",
                reason="no media activity detected",
                trim_start_sec=0.0,
                trim_end_sec=duration,
                duration_sec=duration,
                samples=samples,
                diagnostics=self._diagnostics,
            )

        first_active = active_samples[0].timestamp_sec
        last_active = active_samples[-1].timestamp_sec
        first_active, last_active, refinement = await self._refine_boundaries(
            input_path=input_path,
            duration=duration,
            first_active=first_active,
            last_active=last_active,
        )
        self._diagnostics["refinement"] = refinement
        last_active_end = last_active + self.config.sample_window_sec
        trim_start = max(0.0, first_active - self.config.smart_trim_pre_roll_sec)
        trim_end = min(duration, max(trim_start + 1.0, last_active_end + self.config.smart_trim_end_post_roll_sec))
        status = "trimmed" if trim_start > 0.5 or trim_end < duration - 0.5 else "skipped"
        reason = "media activity boundaries detected" if status == "trimmed" else "no trim needed"
        self._diagnostics["analysis_elapsed_sec"] = round(perf_counter() - started_at, 3)
        return TrimDecision(
            status=status,
            reason=reason,
            trim_start_sec=trim_start,
            trim_end_sec=trim_end,
            duration_sec=duration,
            samples=samples,
            diagnostics=self._diagnostics,
        )

    async def _collect_samples(self, input_path: Path, duration: float) -> list[ActivitySample]:
        timestamps = _sample_timestamps(duration, self.config.sample_interval_sec)
        return await self._collect_samples_for_timestamps(
            input_path=input_path,
            duration=duration,
            timestamps=timestamps,
            phase="coarse",
            sample_interval_sec=self.config.sample_interval_sec,
            probe_start_sec=0.0,
            probe_end_sec=duration,
        )

    async def _collect_samples_for_timestamps(
        self,
        *,
        input_path: Path,
        duration: float,
        timestamps: list[float],
        phase: str,
        sample_interval_sec: float,
        probe_start_sec: float,
        probe_end_sec: float,
    ) -> list[ActivitySample]:
        if not timestamps:
            return []

        async def timed_probe(
            media_type: str,
            probe: Awaitable[list[tuple[bool | None, float | None]]],
        ) -> list[tuple[bool | None, float | None]]:
            started = perf_counter()
            unavailable_reason = None
            try:
                states = await probe
            except Exception as exc:
                logger.debug("%s %s activity probe failed: %s", phase, media_type, exc)
                states = [(None, None) for _ in timestamps]
                unavailable_reason = str(exc)
            available = any(state[0] is not None for state in states)
            if not available and unavailable_reason is None:
                unavailable_reason = "no activity samples available"
            self._diagnostics.setdefault("probes", []).append(
                {
                    "phase": phase,
                    "media_type": media_type,
                    "elapsed_sec": round(perf_counter() - started, 3),
                    "sample_count": len(states),
                    "available": available,
                    "unavailable_reason": unavailable_reason,
                }
            )
            return states

        audio_states, video_states = await asyncio.gather(
            timed_probe(
                "audio",
                self._collect_audio_activity(
                    input_path,
                    duration,
                    timestamps,
                    probe_start_sec=probe_start_sec,
                    probe_end_sec=probe_end_sec,
                ),
            ),
            timed_probe(
                "video",
                self._collect_video_activity(
                    input_path,
                    duration,
                    timestamps,
                    probe_start_sec=probe_start_sec,
                    probe_end_sec=probe_end_sec,
                    sample_interval_sec=sample_interval_sec,
                ),
            ),
        )

        samples: list[ActivitySample] = []
        for index, timestamp in enumerate(timestamps):
            audio_active, audio_level = audio_states[index] if index < len(audio_states) else (None, None)
            video_active, video_diff = video_states[index] if index < len(video_states) else (None, None)
            samples.append(
                ActivitySample(
                    timestamp_sec=timestamp,
                    audio_active=audio_active,
                    video_active=video_active,
                    audio_level_db=audio_level,
                    video_diff=video_diff,
                    reason=f"completed-file {phase} sample",
                )
            )
        return samples

    async def _refine_boundaries(
        self,
        *,
        input_path: Path,
        duration: float,
        first_active: float,
        last_active: float,
    ) -> tuple[float, float, dict[str, Any]]:
        if self.config.sample_interval_sec <= 1.0:
            return (
                first_active,
                last_active,
                {"status": "skipped", "reason": "sample interval already at refinement resolution"},
            )

        coarse_interval = self.config.sample_interval_sec
        refinement_interval = 1.0
        start_range = (
            max(0.0, first_active - coarse_interval),
            min(duration, first_active + coarse_interval + self.config.sample_window_sec),
        )
        end_range = (
            max(0.0, last_active - coarse_interval),
            min(duration, last_active + coarse_interval + self.config.sample_window_sec),
        )

        windows = [start_range]
        if end_range[0] <= start_range[1]:
            windows[0] = (min(start_range[0], end_range[0]), max(start_range[1], end_range[1]))
        else:
            windows.append(end_range)

        refined_samples: list[ActivitySample] = []
        for index, (window_start, window_end) in enumerate(windows):
            timestamps = _sample_timestamps_between(window_start, window_end, refinement_interval)
            samples = await self._collect_samples_for_timestamps(
                input_path=input_path,
                duration=duration,
                timestamps=timestamps,
                phase=f"refine_{index + 1}",
                sample_interval_sec=refinement_interval,
                probe_start_sec=window_start,
                probe_end_sec=window_end,
            )
            refined_samples.extend(samples)

        refined_active = [sample for sample in refined_samples if sample.active is True]
        if not refined_active:
            return (
                first_active,
                last_active,
                {
                    "status": "skipped",
                    "reason": "no refined active samples",
                    "window_count": len(windows),
                    "sample_count": len(refined_samples),
                },
            )

        refined_first = min(sample.timestamp_sec for sample in refined_active)
        refined_last = max(sample.timestamp_sec for sample in refined_active)
        return (
            refined_first,
            refined_last,
            {
                "status": "refined",
                "interval_sec": refinement_interval,
                "window_count": len(windows),
                "sample_count": len(refined_samples),
                "coarse_first_active_sec": first_active,
                "coarse_last_active_sec": last_active,
                "refined_first_active_sec": refined_first,
                "refined_last_active_sec": refined_last,
            },
        )

    async def _probe_duration_sec(self, input_path: Path) -> float | None:
        return await _probe_media_duration_sec(input_path)

    async def _collect_audio_activity(
        self,
        input_path: Path,
        duration: float,
        sample_timestamps: list[float],
        *,
        probe_start_sec: float = 0.0,
        probe_end_sec: float | None = None,
    ) -> list[tuple[bool | None, float | None]]:
        probe_end = min(duration, duration if probe_end_sec is None else probe_end_sec)
        probe_start = max(0.0, min(probe_start_sec, probe_end))
        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
        ]
        if probe_start > 0:
            cmd.extend(("-ss", f"{probe_start:.3f}"))
        cmd.extend(
            [
                "-i",
                str(input_path),
            ]
        )
        if probe_end > probe_start and (probe_start > 0 or probe_end < duration):
            cmd.extend(("-t", f"{probe_end - probe_start:.3f}"))
        cmd.extend(
            [
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-f",
                "s16le",
                "-",
            ]
        )
        timeout_sec = _activity_probe_timeout_sec(duration, probe_start, probe_end)
        empty_result = [(None, None) for _ in sample_timestamps]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.debug("Audio batch probe unavailable for %s: ffmpeg not found", input_path)
            return empty_result

        stderr_task = asyncio.create_task(_read_stderr_limited(process.stderr))
        try:
            async with asyncio.timeout(timeout_sec):
                states = await self._read_audio_activity_stream(
                    process.stdout,
                    sample_timestamps=sample_timestamps,
                    probe_start_sec=probe_start,
                )
                returncode = await process.wait()
        except TimeoutError:
            await _close_process(process)
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            logger.debug("Audio batch probe timed out for %s", input_path)
            return empty_result

        stderr = await stderr_task
        if returncode != 0 or not any(state[0] is not None for state in states):
            logger.debug("Audio batch probe unavailable for %s: %s", input_path, stderr[:200])
            return empty_result
        return states

    async def _read_audio_activity_stream(
        self,
        stream: asyncio.StreamReader | None,
        *,
        sample_timestamps: list[float],
        probe_start_sec: float,
    ) -> list[tuple[bool | None, float | None]]:
        if stream is None:
            return [(None, None) for _ in sample_timestamps]

        window_size = max(1, int(self.config.sample_window_sec * AUDIO_SAMPLE_RATE))
        window_starts = [
            max(0, int((timestamp - probe_start_sec) * AUDIO_SAMPLE_RATE)) for timestamp in sample_timestamps
        ]
        window_ends = [start + window_size for start in window_starts]
        peaks = [0 for _ in sample_timestamps]
        observed = [False for _ in sample_timestamps]
        chunk_start = 0
        remainder = b""

        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            payload = remainder + chunk
            if len(payload) % 2:
                remainder = payload[-1:]
                payload = payload[:-1]
            else:
                remainder = b""
            if not payload:
                continue
            pcm = array("h")
            pcm.frombytes(payload)
            if sys.byteorder != "little":
                pcm.byteswap()
            if not pcm:
                continue
            chunk_end = chunk_start + len(pcm)
            first_window = bisect_right(window_ends, chunk_start)
            index = first_window
            while index < len(sample_timestamps) and window_starts[index] < chunk_end:
                overlap_start = max(window_starts[index], chunk_start)
                overlap_end = min(window_ends[index], chunk_end)
                if overlap_end > overlap_start:
                    rel_start = overlap_start - chunk_start
                    rel_end = overlap_end - chunk_start
                    peaks[index] = max(peaks[index], max(abs(value) for value in pcm[rel_start:rel_end]))
                    observed[index] = True
                index += 1
            chunk_start = chunk_end

        states: list[tuple[bool | None, float | None]] = []
        for index, was_observed in enumerate(observed):
            if not was_observed:
                states.append((None, None))
                continue
            level = _db_from_peak(peaks[index])
            states.append((level >= self.config.audio_threshold_db, level))
        return states

    async def _collect_video_activity(
        self,
        input_path: Path,
        duration: float,
        sample_timestamps: list[float],
        *,
        probe_start_sec: float = 0.0,
        probe_end_sec: float | None = None,
        sample_interval_sec: float | None = None,
    ) -> list[tuple[bool | None, float | None]]:
        probe_end = min(duration, duration if probe_end_sec is None else probe_end_sec)
        probe_start = max(0.0, min(probe_start_sec, probe_end))
        interval = sample_interval_sec or self.config.sample_interval_sec
        fps = 1.0 / interval
        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
        ]
        if probe_start > 0:
            cmd.extend(("-ss", f"{probe_start:.3f}"))
        cmd.extend(
            [
                "-i",
                str(input_path),
            ]
        )
        if probe_end > probe_start and (probe_start > 0 or probe_end < duration):
            cmd.extend(("-t", f"{probe_end - probe_start:.3f}"))
        cmd.extend(
            [
                "-an",
                "-vf",
                f"fps={fps:.6f},scale={VIDEO_SAMPLE_WIDTH}:{VIDEO_SAMPLE_HEIGHT},format=gray",
                "-f",
                "rawvideo",
                "-",
            ]
        )
        timeout_sec = _activity_probe_timeout_sec(duration, probe_start, probe_end)
        empty_result = [(None, None) for _ in sample_timestamps]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.debug("Video batch probe unavailable for %s: ffmpeg not found", input_path)
            return empty_result

        stderr_task = asyncio.create_task(_read_stderr_limited(process.stderr))
        try:
            async with asyncio.timeout(timeout_sec):
                states = await self._read_video_activity_stream(
                    process.stdout,
                    sample_count=len(sample_timestamps),
                )
                returncode = await process.wait()
        except TimeoutError:
            await _close_process(process)
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            logger.debug("Video batch probe timed out for %s", input_path)
            return empty_result

        stderr = await stderr_task
        if returncode != 0 or not any(state[0] is not None for state in states):
            logger.debug("Video batch probe unavailable for %s: %s", input_path, stderr[:200])
            return empty_result

        return states

    async def _read_video_activity_stream(
        self,
        stream: asyncio.StreamReader | None,
        *,
        sample_count: int,
    ) -> list[tuple[bool | None, float | None]]:
        states: list[tuple[bool | None, float | None]] = [(None, None) for _ in range(sample_count)]
        if stream is None:
            return states

        previous_frame: bytes | None = None
        frame_index = 0
        buffer = b""
        while True:
            chunk = await stream.read(VIDEO_FRAME_SIZE * 4)
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= VIDEO_FRAME_SIZE:
                frame = buffer[:VIDEO_FRAME_SIZE]
                buffer = buffer[VIDEO_FRAME_SIZE:]
                if previous_frame is None:
                    previous_frame = frame
                    frame_index += 1
                    continue
                diff = _byte_diff_ratio(previous_frame, frame)
                _apply_video_transition(
                    states,
                    index=frame_index,
                    diff=diff,
                    threshold=self.config.video_diff_threshold,
                )
                previous_frame = frame
                frame_index += 1
        return states


async def trim_recording(
    *,
    input_path: Path,
    output_path: Path,
    trim_start_sec: float,
    trim_end_sec: float,
    log_path: Path | None = None,
) -> RecordingInfo | None:
    """Create a trimmed recording output using stream copy."""
    if not input_path.exists():
        logger.warning("Trim skipped, input not found: %s", input_path)
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    expected_duration_sec = max(0.0, trim_end_sec - trim_start_sec)
    cmd = (
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-y",
        "-ss",
        f"{trim_start_sec:.3f}",
        "-t",
        f"{expected_duration_sec:.3f}",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output_path),
    )
    started_at = utc_now()
    returncode, stderr = await _run_ffmpeg_trim(
        *cmd,
        timeout_sec=max(60.0, expected_duration_sec * 2),
        log_path=log_path,
    )
    if returncode != 0 or not output_path.exists():
        logger.error("Trim failed for %s: %s", input_path, stderr[:400])
        return None
    duration_sec = await _probe_media_duration_sec(output_path)
    if duration_sec is None:
        duration_sec = expected_duration_sec
    return RecordingInfo(
        output_path=output_path,
        file_size=output_path.stat().st_size,
        duration_sec=duration_sec,
        start_time=started_at,
        end_time=started_at + timedelta(seconds=duration_sec),
    )
