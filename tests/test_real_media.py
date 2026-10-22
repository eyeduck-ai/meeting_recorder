"""Small, deterministic FFmpeg files exercise the actual trim/probe/remux contract."""

import os
import shutil

import pytest

from recording.activity import ActivityConfig, RecordingActivityAnalyzer, trim_recording
from recording.remux import ensure_canonical_mp4
from recording.subprocess_utils import run_bounded_subprocess
from scripts.media_validation import validate_smoke_media


@pytest.fixture(autouse=True)
def require_tools():
    if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
        if os.getenv("REQUIRE_MEDIA_TOOLS") == "1":
            pytest.fail("CI requires FFmpeg/ffprobe")
        pytest.skip("FFmpeg/ffprobe not installed")


async def _sample(path, *, sound: bool, moving: bool = False, duration: int = 30, tail_motion: bool = False):
    picture = f"testsrc2=s=320x180:r=10:d={duration}" if moving else f"color=c=0x204060:s=320x180:r=10:d={duration}"
    if tail_motion:
        picture += f",drawbox=x=0:y=0:w=160:h=180:color=white:t=fill:enable=gte(t\\,{duration - 1})"
    audio = (
        f"aevalsrc=0.3*sin(2*PI*440*t)*between(t\\,8\\,20):s=48000:d={duration}"
        if sound
        else f"anullsrc=r=48000:cl=mono:d={duration}"
    )
    result = await run_bounded_subprocess(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        picture,
        "-f",
        "lavfi",
        "-i",
        audio,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-g",
        "10",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
        timeout_sec=45,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [30, 31])
async def test_real_static_silent_media_is_not_activity(tmp_path, duration):
    path = tmp_path / "silent.mkv"
    await _sample(path, sound=False, duration=duration)
    decision = await RecordingActivityAnalyzer(ActivityConfig(sample_interval_sec=2)).analyze(path)
    assert decision.status == "skipped"
    assert "no media activity" in decision.reason
    assert all(sample.active is False for sample in decision.samples)
    with pytest.raises(ValueError, match="silent"):
        await validate_smoke_media(path, expected_duration=duration)


@pytest.mark.asyncio
async def test_real_activity_boundaries_trim_duration_and_mp4(tmp_path):
    path = tmp_path / "original.mkv"
    await _sample(path, sound=True)
    config = ActivityConfig(sample_interval_sec=2, smart_trim_pre_roll_sec=1, smart_trim_end_post_roll_sec=1)
    decision = await RecordingActivityAnalyzer(config).analyze(path)
    assert decision.should_trim, decision
    assert 5 <= decision.trim_start_sec <= 9
    assert 20 <= decision.trim_end_sec <= 24
    trimmed = await trim_recording(
        input_path=path,
        output_path=tmp_path / "trimmed.mkv",
        trim_start_sec=decision.trim_start_sec,
        trim_end_sec=decision.trim_end_sec,
    )
    assert trimmed is not None
    assert abs(trimmed.duration_sec - (decision.trim_end_sec - decision.trim_start_sec)) <= 2
    canonical = await ensure_canonical_mp4(trimmed.output_path)
    assert canonical and canonical.suffix == ".mp4"
    assert path.exists(), "Original must survive smart trim and upload preparation"
    with pytest.raises(ValueError, match="frozen"):
        await validate_smoke_media(canonical, expected_duration=trimmed.duration_sec)


@pytest.mark.asyncio
async def test_real_animated_media_passes_smoke_validation(tmp_path):
    path = tmp_path / "moving.mkv"
    await _sample(path, sound=True, moving=True)
    result = await validate_smoke_media(path, expected_duration=30)
    assert result["streams"] == ["audio", "video"]
    assert result["mean_volume_db"] > -55


@pytest.mark.asyncio
async def test_corrupt_media_retains_original(tmp_path):
    path = tmp_path / "broken.mkv"
    path.write_bytes(b"not a valid recording")
    decision = await RecordingActivityAnalyzer(ActivityConfig()).analyze(path)
    assert not decision.should_trim
    assert path.read_bytes() == b"not a valid recording"
    assert await ensure_canonical_mp4(path) is None


@pytest.mark.asyncio
async def test_real_terminal_frame_is_observed_for_activity(tmp_path):
    path = tmp_path / "tail.mkv"
    await _sample(path, sound=False, tail_motion=True)
    decision = await RecordingActivityAnalyzer(ActivityConfig(sample_interval_sec=2)).analyze(path)
    assert decision.samples[-1].timestamp_sec == 30
    assert decision.samples[-1].video_active is True
    assert decision.samples[-1].audio_active is False
    assert all(sample.active is not None for sample in decision.samples)


@pytest.mark.asyncio
async def test_real_missing_audio_stream_still_retains_original(tmp_path):
    source = tmp_path / "source.mkv"
    path = tmp_path / "video-only.mkv"
    await _sample(source, sound=False)
    result = await run_bounded_subprocess(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        str(path),
        timeout_sec=30,
    )
    assert result.returncode == 0, result.stderr
    decision = await RecordingActivityAnalyzer(ActivityConfig(sample_interval_sec=2)).analyze(path)
    assert not decision.should_trim
    assert all(sample.audio_active is None for sample in decision.samples)
    assert all(sample.active is None for sample in decision.samples)
    assert decision.trim_start_sec == 0
    assert decision.trim_end_sec == decision.duration_sec
