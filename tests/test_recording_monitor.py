"""Tests for the recording monitor loop."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from recording.activity import ActivityConfig, LiveMediaActivityProbe, MediaActivityState
from recording.monitor import RecordingMonitor
from utils.timezone import utc_now


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
        self.detect_meeting_end_calls = 0

    def process_returncode(self):
        return self._process_returncode

    async def detect_meeting_end(self, stage):
        self.detect_meeting_end_calls += 1
        assert stage == "monitor_recording"
        return self._meeting_ended


def make_job(**kwargs):
    defaults = {
        "duration_sec": 60,
        "dynamic_extension_enabled": False,
        "dynamic_extension_idle_sec": 300,
        "dynamic_extension_max_sec": 3600,
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
    media_activity_probe=None,
    cancel=False,
    finish=False,
    clock=None,
    wall_clock=None,
    ffmpeg_stall_timeout_sec=120,
    ffmpeg_stall_grace_sec=30,
):
    return RecordingMonitor(
        session=session or FakeSession(),
        job=job or make_job(),
        media_activity_probe=media_activity_probe,
        is_cancel_requested=lambda: cancel,
        is_finish_requested=lambda: finish,
        ffmpeg_stall_timeout_sec=ffmpeg_stall_timeout_sec,
        ffmpeg_stall_grace_sec=ffmpeg_stall_grace_sec,
        clock=clock or sequence_clock(0, 0),
        wall_clock=wall_clock,
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


class FakeActivityProbe:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0
        self.prime_calls = 0
        self.close_calls = 0

    async def prime(self, session):
        self.prime_calls += 1

    async def check(self, session):
        self.calls += 1
        if self.states:
            return self.states.pop(0)
        return MediaActivityState(audio_active=False, video_active=False, reason="default idle")

    async def close(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_monitor_dynamic_extension_continues_until_max_extension():
    probe = FakeActivityProbe(
        MediaActivityState(audio_active=True, video_active=False, reason="audio active"),
        MediaActivityState(audio_active=False, video_active=True, reason="video active"),
    )
    monitor = make_monitor(
        job=make_job(
            duration_sec=5,
            dynamic_extension_enabled=True,
            dynamic_extension_idle_sec=10,
            dynamic_extension_max_sec=10,
        ),
        media_activity_probe=probe,
        clock=sequence_clock(0, 6, 11, 16),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert monitor.dynamic_extension_stop_reason == "max_extension_reached"
    assert probe.calls == 2
    assert probe.close_calls == 1


@pytest.mark.asyncio
async def test_monitor_dynamic_extension_stops_after_idle_timeout():
    probe = FakeActivityProbe(
        MediaActivityState(audio_active=False, video_active=False, reason="idle"),
        MediaActivityState(audio_active=False, video_active=False, reason="idle"),
        MediaActivityState(audio_active=False, video_active=False, reason="idle"),
    )
    monitor = make_monitor(
        job=make_job(
            duration_sec=5,
            dynamic_extension_enabled=True,
            dynamic_extension_idle_sec=10,
            dynamic_extension_max_sec=60,
        ),
        media_activity_probe=probe,
        clock=sequence_clock(0, 6, 11, 17),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert monitor.dynamic_extension_stop_reason == "idle_timeout"


@pytest.mark.asyncio
async def test_monitor_dynamic_extension_falls_back_when_probes_unavailable():
    probe = FakeActivityProbe(
        MediaActivityState(audio_active=None, video_active=None, reason="baseline"),
        MediaActivityState(audio_active=None, video_active=None, reason="unavailable"),
    )
    monitor = make_monitor(
        job=make_job(duration_sec=5, dynamic_extension_enabled=True),
        media_activity_probe=probe,
        clock=sequence_clock(0, 6, 11),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert monitor.dynamic_extension_stop_reason == "activity_probe_unavailable"
    assert probe.close_calls == 1


@pytest.mark.asyncio
async def test_monitor_primes_dynamic_extension_probe_before_duration():
    probe = FakeActivityProbe(
        MediaActivityState(audio_active=None, video_active=None, reason="baseline"),
        MediaActivityState(audio_active=None, video_active=None, reason="unavailable"),
    )
    monitor = make_monitor(
        job=make_job(duration_sec=10, dynamic_extension_enabled=True),
        media_activity_probe=probe,
        clock=sequence_clock(0, 6, 11, 16),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert probe.prime_calls == 1
    assert probe.close_calls == 1


@pytest.mark.asyncio
async def test_monitor_closes_dynamic_extension_probe_on_cancel():
    probe = FakeActivityProbe()
    monitor = make_monitor(
        job=make_job(duration_sec=10, dynamic_extension_enabled=True),
        media_activity_probe=probe,
        cancel=True,
        clock=sequence_clock(0),
    )

    with pytest.raises(asyncio.CancelledError):
        await monitor.run()

    assert probe.close_calls == 1


@pytest.mark.asyncio
async def test_monitor_stops_at_hard_deadline_before_dynamic_extension_can_extend():
    now = utc_now()
    wall_times = [now, now + timedelta(seconds=2)]

    def wall_clock():
        if wall_times:
            return wall_times.pop(0)
        return now + timedelta(seconds=2)

    probe = FakeActivityProbe(MediaActivityState(audio_active=True, video_active=True, reason="active"))
    monitor = make_monitor(
        job=make_job(
            duration_sec=60,
            dynamic_extension_enabled=True,
            dynamic_extension_max_sec=3600,
            hard_deadline_at=now + timedelta(seconds=1),
        ),
        media_activity_probe=probe,
        clock=sequence_clock(0, 2),
        wall_clock=wall_clock,
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert monitor.dynamic_extension_stop_reason == "hard_deadline_reached"
    assert probe.calls == 0
    assert probe.close_calls == 1


@pytest.mark.asyncio
async def test_monitor_extends_when_audio_unavailable_but_video_active():
    probe = FakeActivityProbe(MediaActivityState(audio_active=None, video_active=True, reason="video active"))
    monitor = make_monitor(
        job=make_job(duration_sec=5, dynamic_extension_enabled=True, dynamic_extension_max_sec=5),
        media_activity_probe=probe,
        clock=sequence_clock(0, 6, 11),
    )

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert monitor.dynamic_extension_stop_reason == "max_extension_reached"
    assert probe.calls == 1


@pytest.mark.asyncio
async def test_live_activity_probe_checks_audio_and_video_concurrently():
    probe = LiveMediaActivityProbe(ActivityConfig())
    audio_started = asyncio.Event()
    video_started = asyncio.Event()

    async def check_audio(session):
        audio_started.set()
        await video_started.wait()
        return True, -20.0

    async def check_video(session):
        await audio_started.wait()
        video_started.set()
        return False, 0.0

    probe._check_audio = check_audio
    probe._check_video = check_video

    state = await asyncio.wait_for(probe.check(FakeSession()), timeout=1.0)

    assert state.audio_active is True
    assert state.video_active is False


@pytest.mark.asyncio
async def test_live_activity_probe_prime_warms_audio_and_video():
    probe = LiveMediaActivityProbe(ActivityConfig())
    calls = []

    async def check_audio(session):
        calls.append("audio")
        return None, None

    async def check_video(session):
        calls.append("video")
        return None, None

    probe._check_audio = check_audio
    probe._check_video = check_video

    await probe.prime(FakeSession())

    assert sorted(calls) == ["audio", "video"]


@pytest.mark.asyncio
async def test_monitor_ignores_provider_meeting_end_probe():
    session = FakeSession(meeting_ended=True)
    monitor = make_monitor(session=session, job=make_job(duration_sec=5), clock=sequence_clock(0, 1, 6))

    end_reason, ffmpeg_exit_code = await monitor.run()

    assert end_reason == "completed"
    assert ffmpeg_exit_code is None
    assert session.detect_meeting_end_calls == 0
