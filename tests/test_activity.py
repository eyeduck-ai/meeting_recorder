"""Tests for media activity boundary decisions."""

import asyncio
from array import array
from pathlib import Path

import pytest

from recording.activity import (
    AUDIO_SAMPLE_RATE,
    VIDEO_FRAME_SIZE,
    ActivityConfig,
    RecordingActivityAnalyzer,
    _video_activity_from_frames,
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
