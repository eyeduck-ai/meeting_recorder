"""Tests for media activity boundary decisions."""

import asyncio
from array import array
from pathlib import Path

import pytest

import recording.activity as activity_module
from recording.activity import (
    AUDIO_SAMPLE_RATE,
    VIDEO_FRAME_SIZE,
    ActivityConfig,
    RecordingActivityAnalyzer,
    _video_activity_from_frames,
    trim_recording,
)


class FakeAnalyzer(RecordingActivityAnalyzer):
    def __init__(self, audio_states, video_states=None, *, duration=60.0, config=None):
        super().__init__(config or ActivityConfig(sample_interval_sec=5.0, smart_trim_pre_roll_sec=2.0))
        self._audio_states = audio_states
        self._video_states = video_states or [(False, 0.0) for _ in audio_states]
        self._duration = duration
        self.audio_batch_calls = 0
        self.video_batch_calls = 0

    async def _probe_duration_sec(self, input_path: Path) -> float | None:
        return self._duration

    async def _collect_audio_activity(
        self, input_path: Path, duration: float, sample_timestamps: list[float], **kwargs
    ):
        self.audio_batch_calls += 1
        return self._audio_states

    async def _collect_video_activity(
        self, input_path: Path, duration: float, sample_timestamps: list[float], **kwargs
    ):
        self.video_batch_calls += 1
        return self._video_states

    async def _refine_boundaries(self, *, input_path: Path, duration: float, first_active: float, last_active: float):
        return first_active, last_active, {"status": "skipped", "reason": "fake analyzer"}


class TimestampActivityAnalyzer(RecordingActivityAnalyzer):
    def __init__(self):
        super().__init__(
            ActivityConfig(
                sample_interval_sec=5.0,
                sample_window_sec=1.0,
                smart_trim_pre_roll_sec=2.0,
                smart_trim_end_post_roll_sec=5.0,
            )
        )
        self.audio_batch_calls = 0
        self.video_batch_calls = 0

    async def _probe_duration_sec(self, input_path: Path) -> float | None:
        return 60.0

    async def _collect_audio_activity(
        self, input_path: Path, duration: float, sample_timestamps: list[float], **kwargs
    ):
        self.audio_batch_calls += 1
        return [
            (12.0 <= timestamp < 52.0, -20.0 if 12.0 <= timestamp < 52.0 else -120.0) for timestamp in sample_timestamps
        ]

    async def _collect_video_activity(
        self, input_path: Path, duration: float, sample_timestamps: list[float], **kwargs
    ):
        self.video_batch_calls += 1
        return [(False, 0.0) for _ in sample_timestamps]


@pytest.mark.asyncio
async def test_leading_silence_and_still_video_trims_start():
    analyzer = FakeAnalyzer(
        [
            (False, -120.0),
            (False, -120.0),
            (True, -20.0),
            (True, -20.0),
        ]
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "trimmed"
    assert decision.trim_start_sec == 8.0
    assert decision.trim_end_sec == 21.0


@pytest.mark.asyncio
async def test_audio_only_activity_starts_output():
    analyzer = FakeAnalyzer(
        [
            (False, -120.0),
            (True, -20.0),
        ]
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "trimmed"
    assert decision.trim_start_sec == 3.0


@pytest.mark.asyncio
async def test_video_only_activity_starts_output():
    analyzer = FakeAnalyzer(
        [(False, -120.0), (False, -120.0)],
        [(False, 0.0), (True, 0.2)],
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "trimmed"
    assert decision.trim_start_sec == 3.0


@pytest.mark.asyncio
async def test_no_confident_activity_leaves_raw_output_untrimmed():
    analyzer = FakeAnalyzer(
        [
            (False, -120.0),
            (False, -120.0),
        ]
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "skipped"
    assert decision.should_trim is False
    assert decision.reason == "no media activity detected"


@pytest.mark.asyncio
async def test_unavailable_probes_leave_raw_output_untrimmed():
    analyzer = FakeAnalyzer(
        [
            (None, None),
            (None, None),
        ],
        [(None, None), (None, None)],
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "skipped"
    assert decision.should_trim is False
    assert decision.reason == "media probes unavailable"


@pytest.mark.asyncio
async def test_analyzer_uses_batch_collection_once_per_media_type():
    analyzer = FakeAnalyzer([(False, -120.0), (True, -20.0)])

    await analyzer.analyze(Path("recording.mkv"))

    assert analyzer.audio_batch_calls == 1
    assert analyzer.video_batch_calls == 1


def test_video_activity_transition_marks_previous_sample_active():
    frame_a = bytes([0] * 8)
    frame_b = bytes([255] * 8)

    states = _video_activity_from_frames([frame_a, frame_b], sample_count=2, threshold=0.1)

    assert states[0][0] is True
    assert states[1][0] is True


@pytest.mark.asyncio
async def test_video_only_activity_beginning_at_zero_keeps_zero_start():
    analyzer = FakeAnalyzer(
        [(False, -120.0), (False, -120.0)],
        [(True, 0.5), (True, 0.5)],
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "trimmed"
    assert decision.trim_start_sec == 0.0


@pytest.mark.asyncio
async def test_audio_stream_reader_maps_peak_windows():
    analyzer = RecordingActivityAnalyzer(ActivityConfig(audio_threshold_db=-45.0, sample_window_sec=1.0))
    samples = array("h", [0] * AUDIO_SAMPLE_RATE + [12000] * AUDIO_SAMPLE_RATE + [0] * AUDIO_SAMPLE_RATE)
    reader = asyncio.StreamReader()
    reader.feed_data(samples.tobytes())
    reader.feed_eof()

    states = await analyzer._read_audio_activity_stream(
        reader,
        sample_timestamps=[0.0, 1.0, 2.0],
        probe_start_sec=0.0,
    )

    assert states[0][0] is False
    assert states[1][0] is True
    assert states[2][0] is False


@pytest.mark.asyncio
async def test_video_stream_reader_compares_frames_without_buffering_all_frames():
    analyzer = RecordingActivityAnalyzer(ActivityConfig(video_diff_threshold=0.1))
    frame_a = bytes([0] * VIDEO_FRAME_SIZE)
    frame_b = bytes([255] * VIDEO_FRAME_SIZE)
    reader = asyncio.StreamReader()
    reader.feed_data(frame_a + frame_b)
    reader.feed_eof()

    states = await analyzer._read_video_activity_stream(reader, sample_count=2)

    assert states[0][0] is True
    assert states[1][0] is True


@pytest.mark.asyncio
async def test_coarse_to_fine_refinement_tightens_trim_boundaries():
    analyzer = TimestampActivityAnalyzer()

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.status == "trimmed"
    assert decision.trim_start_sec == 10.0
    assert decision.trim_end_sec == 57.0
    assert decision.diagnostics["refinement"]["status"] == "refined"
    assert analyzer.audio_batch_calls == 3
    assert analyzer.video_batch_calls == 3


@pytest.mark.asyncio
async def test_trim_recording_uses_duration_and_reports_actual_duration(monkeypatch, tmp_path):
    input_path = tmp_path / "raw.mkv"
    output_path = tmp_path / "trimmed.mkv"
    input_path.write_bytes(b"raw")
    commands = []

    async def fake_run_ffmpeg_trim(*cmd, timeout_sec, log_path=None):
        commands.append(cmd)
        output_path.write_bytes(b"trimmed")
        return 0, ""

    async def fake_run_ffmpeg_probe(*cmd, timeout_sec=10.0):
        assert cmd[0] == "ffprobe"
        return 0, b"8.750", ""

    monkeypatch.setattr(activity_module, "_run_ffmpeg_trim", fake_run_ffmpeg_trim)
    monkeypatch.setattr(activity_module, "_run_ffmpeg_probe", fake_run_ffmpeg_probe)

    info = await trim_recording(
        input_path=input_path,
        output_path=output_path,
        trim_start_sec=2.0,
        trim_end_sec=10.0,
    )

    assert info is not None
    trim_cmd = commands[0]
    assert "-hide_banner" in trim_cmd
    assert "-nostats" in trim_cmd
    assert trim_cmd[trim_cmd.index("-ss") + 1] == "2.000"
    assert trim_cmd[trim_cmd.index("-t") + 1] == "8.000"
    assert "-to" not in trim_cmd
    assert info.duration_sec == 8.75


@pytest.mark.asyncio
async def test_trim_runner_streams_output_without_communicate(monkeypatch, tmp_path):
    log_path = tmp_path / "trim.log"
    calls = []

    class FakeStream:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        async def read(self, _size):
            if self.chunks:
                return self.chunks.pop(0)
            return b""

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStream([b"stdout"])
            self.stderr = FakeStream([b"stderr"])
            self.returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

        async def communicate(self):
            raise AssertionError("trim runner must not use communicate")

    async def fake_exec(*cmd, stdout=None, stderr=None):
        calls.append((cmd, stdout, stderr))
        return FakeProcess()

    monkeypatch.setattr(activity_module.asyncio, "create_subprocess_exec", fake_exec)

    returncode, excerpt = await activity_module._run_ffmpeg_trim(
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        timeout_sec=1.0,
        log_path=log_path,
    )

    assert returncode == 0
    assert excerpt == "stderr"
    assert log_path.read_text(encoding="utf-8") == "stderr"
    assert calls[0][0][:3] == ("ffmpeg", "-hide_banner", "-nostats")


@pytest.mark.asyncio
async def test_trim_runner_timeout_terminates_process(monkeypatch):
    process = None

    class FakeStream:
        async def read(self, _size):
            return b""

    class SlowProcess:
        def __init__(self):
            self.stdout = FakeStream()
            self.stderr = FakeStream()
            self.returncode = None
            self.terminated = False
            self.killed = False

        async def wait(self):
            if self.terminated or self.killed:
                self.returncode = 143
                return 143
            await asyncio.sleep(10)
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    async def fake_exec(*_cmd, stdout=None, stderr=None):
        nonlocal process
        process = SlowProcess()
        return process

    monkeypatch.setattr(activity_module.asyncio, "create_subprocess_exec", fake_exec)

    returncode, excerpt = await activity_module._run_ffmpeg_trim("ffmpeg", timeout_sec=0.001)

    assert returncode == 124
    assert process is not None
    assert process.terminated is True
    assert "trim timed out" in excerpt
