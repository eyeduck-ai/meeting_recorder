"""Tests for the recording monitor loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from recording.monitor import RecordingMonitor


class FakeOutputFile:
    def __init__(self, size: int = 0):
        self._size = size

    def exists(self) -> bool:
        return True

    def stat(self):
        return SimpleNamespace(st_size=self._size)


class FakeSession:
    def __init__(self, output_file=None, *, process_returncode=None, meeting_ended=False):
        self.output_file = output_file or FakeOutputFile()
        self.page = object()
        self._process_returncode = process_returncode
        self._meeting_ended = meeting_ended

    def process_returncode(self):
        return self._process_returncode

    async def detect_meeting_end(self, stage):
        assert stage == "monitor_recording"
        return self._meeting_ended


def make_job(**kwargs):
    defaults = {
        "duration_sec": 60,
        "min_duration_sec": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def sequence_clock(*values):
    remaining = list(values)

    def clock():
        if remaining:
            return remaining.pop(0)
        return values[-1]

    return clock


def make_monitor(
    *,
    session=None,
    job=None,
    detection_orchestrator=None,
    cancel=False,
    finish=False,
    clock=None,
    ffmpeg_stall_timeout_sec=120,
    ffmpeg_stall_grace_sec=30,
):
    return RecordingMonitor(
        session=session or FakeSession(),
        job=job or make_job(),
        detection_orchestrator=detection_orchestrator,
        is_cancel_requested=lambda: cancel,
        is_finish_requested=lambda: finish,
        ffmpeg_stall_timeout_sec=ffmpeg_stall_timeout_sec,
        ffmpeg_stall_grace_sec=ffmpeg_stall_grace_sec,
        clock=clock or sequence_clock(0, 0),
        sleep=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_monitor_completes_when_duration_reached():
    monitor = make_monitor(job=make_job(duration_sec=5), clock=sequence_clock(0, 6))

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None


@pytest.mark.asyncio
async def test_monitor_finishes_early_when_finish_requested():
    monitor = make_monitor(finish=True, clock=sequence_clock(0, 1))

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None


@pytest.mark.asyncio
async def test_monitor_raises_cancelled_when_cancel_requested():
    monitor = make_monitor(cancel=True, clock=sequence_clock(0, 1))

    with pytest.raises(asyncio.CancelledError):
        await monitor.run()


@pytest.mark.asyncio
async def test_monitor_raises_when_ffmpeg_output_stalls():
    monitor = make_monitor(
        session=FakeSession(output_file=FakeOutputFile(size=0)),
        job=make_job(duration_sec=120),
        clock=sequence_clock(0, 40),
        ffmpeg_stall_timeout_sec=10,
        ffmpeg_stall_grace_sec=30,
    )

    with pytest.raises(RuntimeError, match="FFmpeg output stalled"):
        await monitor.run()


@pytest.mark.asyncio
async def test_monitor_returns_auto_detected_from_detection_orchestrator():
    detection_result = SimpleNamespace(detected=True, reason="meeting ended")
    detector = SimpleNamespace(check_all=AsyncMock(return_value=(True, [detection_result])))
    monitor = make_monitor(
        job=make_job(duration_sec=120, min_duration_sec=0),
        detection_orchestrator=detector,
        clock=sequence_clock(0, 1),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "auto_detected"
    assert ffmpeg_exit_code is None
    detector.check_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_monitor_returns_auto_detected_from_session_probe():
    monitor = make_monitor(
        session=FakeSession(meeting_ended=True),
        job=make_job(duration_sec=120, min_duration_sec=0),
        clock=sequence_clock(0, 1),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "auto_detected"
    assert ffmpeg_exit_code is None
