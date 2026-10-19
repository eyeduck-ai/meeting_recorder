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
    ActivitySample,
    MediaActivityState,
    RecordingActivityAnalyzer,
    _video_activity_from_frames,
    trim_recording,
)
from recording.subprocess_utils import BoundedSubprocessResult


class FakeAnalyzer(RecordingActivityAnalyzer):
    def __init__(self, audio_states, video_states=None, *, duration=60.0, config=None):
        super().__init__(
            config
            or ActivityConfig(sample_interval_sec=5.0, smart_trim_pre_roll_sec=2.0, smart_trim_end_post_roll_sec=5.0)
        )
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
        padding = (None, None) if all(state[0] is None for state in self._audio_states) else (False, -120.0)
        return [
            self._audio_states[i] if i < len(self._audio_states) else padding for i in range(len(sample_timestamps))
        ]

    async def _collect_video_activity(
        self, input_path: Path, duration: float, sample_timestamps: list[float], **kwargs
    ):
        self.video_batch_calls += 1
        padding = (None, None) if all(state[0] is None for state in self._video_states) else (False, 0.0)
        return [
            self._video_states[i] if i < len(self._video_states) else padding for i in range(len(sample_timestamps))
        ]

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


class BoundaryEvidenceAnalyzer(RecordingActivityAnalyzer):
    def __init__(self, *, duration, coarse_active, refined_active):
        super().__init__(ActivityConfig(smart_trim_pre_roll_sec=2.0, smart_trim_end_post_roll_sec=5.0))
        self.duration = duration
        self.coarse_active = set(coarse_active)
        self.refined_active = set(refined_active)

    async def _probe_duration_sec(self, input_path):
        return self.duration

    async def _collect_samples_for_timestamps(self, *, timestamps, phase, **kwargs):
        active = self.coarse_active if phase == "coarse" else self.refined_active
        return [
            ActivitySample(timestamp_sec=timestamp, audio_active=timestamp in active, video_active=False)
            for timestamp in timestamps
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_active", "refined_active", "expected_end"),
    [(4110.0, {4106.0, 4112.0}, 4118.0), (4070.0, {4067.0, 4068.0}, 4074.0)],
)
async def test_missing_start_refinement_does_not_replace_start_with_end_window(
    last_active, refined_active, expected_end
):
    analyzer = BoundaryEvidenceAnalyzer(
        duration=4482.0, coarse_active={0.0, last_active}, refined_active=refined_active
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.trim_start_sec == 0.0
    assert decision.trim_end_sec == expected_end
    assert decision.diagnostics["refinement"]["start_status"] == "coarse_retained"


@pytest.mark.asyncio
async def test_missing_end_refinement_retains_the_last_coarse_activity():
    analyzer = BoundaryEvidenceAnalyzer(duration=4500.0, coarse_active={100.0, 4000.0}, refined_active={100.0, 101.0})

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.trim_start_sec == 98.0
    assert decision.trim_end_sec == 4006.0
    assert decision.diagnostics["refinement"]["end_status"] == "coarse_retained"


@pytest.mark.asyncio
async def test_overlapping_refinement_windows_refine_both_boundaries():
    analyzer = BoundaryEvidenceAnalyzer(duration=60.0, coarse_active={10.0, 15.0}, refined_active={12.0, 17.0})

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.trim_start_sec == 10.0
    assert decision.trim_end_sec == 23.0
    assert decision.diagnostics["refinement"]["window_count"] == 1


@pytest.mark.asyncio
async def test_implausibly_short_trim_of_long_recording_retains_raw():
    states = [(False, -120.0)] * 900
    states[820] = (True, -20.0)
    analyzer = FakeAnalyzer(states, duration=4500.0)

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.should_trim is False
    assert decision.trim_start_sec == 0.0
    assert decision.trim_end_sec == 4500.0
    assert "too short" in decision.reason
    assert decision.diagnostics["trim_safety"]["candidate_duration_sec"] == 8.0


@pytest.mark.asyncio
async def test_short_recording_can_still_trim_a_short_valid_section():
    analyzer = FakeAnalyzer(
        [(False, -120.0), (False, -120.0), (True, -20.0), (False, -120.0), (False, -120.0), (False, -120.0)],
        duration=30.0,
    )

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.should_trim is True
    assert decision.trim_start_sec == 8.0
    assert decision.trim_end_sec == 16.0


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

    async def fake_run_bounded_subprocess(*cmd, timeout_sec, stdout_limit=4096, stderr_limit=4096, **_kwargs):
        assert cmd[0] == "ffprobe"
        assert timeout_sec == 10.0
        assert stdout_limit == 1024
        assert stderr_limit == 2048
        return BoundedSubprocessResult(returncode=0, stdout=b"8.750", stderr="")

    monkeypatch.setattr(activity_module, "_run_ffmpeg_trim", fake_run_ffmpeg_trim)
    monkeypatch.setattr(activity_module, "run_bounded_subprocess", fake_run_bounded_subprocess)

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
async def test_activity_duration_probe_failure_returns_none(monkeypatch, tmp_path):
    input_path = tmp_path / "recording.mkv"
    input_path.write_bytes(b"raw")

    async def fake_run_bounded_subprocess(*cmd, **_kwargs):
        assert cmd[0] == "ffprobe"
        return BoundedSubprocessResult(returncode=124, stdout=b"", stderr="process timed out", timed_out=True)

    monkeypatch.setattr(activity_module, "run_bounded_subprocess", fake_run_bounded_subprocess)

    assert await activity_module._probe_media_duration_sec(input_path) is None


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


@pytest.mark.asyncio
async def test_default_margins_keep_fifteen_seconds_before_and_thirty_after_activity():
    states = [(100.0 <= index * 5.0 <= 200.0, -20.0) for index in range(60)]
    analyzer = FakeAnalyzer(states, duration=300.0, config=ActivityConfig())

    decision = await analyzer.analyze(Path("recording.mkv"))

    assert decision.trim_start_sec == 85.0
    assert decision.trim_end_sec == 231.0


@pytest.mark.parametrize(
    ("audio", "video", "expected"),
    [
        (True, None, True),
        (None, True, True),
        (True, False, True),
        (False, True, True),
        (False, False, False),
        (None, False, None),
        (False, None, None),
        (None, None, None),
    ],
)
def test_activity_requires_both_probes_to_confirm_inactivity(audio, video, expected):
    state = MediaActivityState(audio_active=audio, video_active=video)
    assert state.active is expected
    assert state.available is (expected is not None)


@pytest.mark.asyncio
async def test_incomplete_activity_samples_retain_the_complete_original():
    analyzer = FakeAnalyzer(
        [(False, -120.0), (True, -20.0), (None, None), (False, -120.0)],
        duration=60.0,
    )
    decision = await analyzer.analyze(Path("recording.mkv"))
    assert decision.should_trim is False
    assert decision.trim_start_sec == 0.0
    assert decision.trim_end_sec == 60.0
    assert decision.diagnostics["trim_safety"]["reason"] == "incomplete media activity evidence"


@pytest.mark.asyncio
async def test_inconclusive_refinement_retains_its_coarse_boundary():
    class IncompleteRefinement(BoundaryEvidenceAnalyzer):
        async def _collect_samples_for_timestamps(self, *, phase, **kwargs):
            samples = await super()._collect_samples_for_timestamps(phase=phase, **kwargs)
            if phase == "refine_1":
                samples[0] = ActivitySample(
                    timestamp_sec=samples[0].timestamp_sec, audio_active=None, video_active=False
                )
            return samples

    analyzer = IncompleteRefinement(duration=4500.0, coarse_active={100.0, 4000.0}, refined_active={103.0, 4001.0})
    decision = await analyzer.analyze(Path("recording.mkv"))
    assert decision.trim_start_sec == 98.0
    assert decision.diagnostics["refinement"]["start_status"] == "coarse_retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_duration", [None, 0.0, float("nan"), 10.0, 90.0])
async def test_invalid_trim_output_is_rejected_and_original_preserved(monkeypatch, tmp_path, actual_duration):
    source = tmp_path / "raw.mkv"
    target = tmp_path / "trimmed.mkv"
    source.write_bytes(b"original")

    async def run(*args, **kwargs):
        target.write_bytes(b"truncated output")
        return 0, ""

    async def duration(_path):
        return actual_duration

    monkeypatch.setattr(activity_module, "_run_ffmpeg_trim", run)
    monkeypatch.setattr(activity_module, "_probe_media_duration_sec", duration)
    result = await trim_recording(input_path=source, output_path=target, trim_start_sec=5.0, trim_end_sec=65.0)
    assert result is None
    assert source.read_bytes() == b"original"
    assert not target.exists()
