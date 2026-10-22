"""Real-media assertions for deterministic smoke tests, using bounded subprocesses."""

import json
import re
from pathlib import Path

from recording.subprocess_utils import run_bounded_subprocess


async def validate_smoke_media(path: Path, *, expected_duration: float) -> dict:
    probe = await run_bounded_subprocess(
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
        timeout_sec=15,
        stdout_limit=65536,
    )
    if probe.returncode:
        raise ValueError("Smoke file cannot be probed")
    metadata = json.loads(probe.stdout)
    duration = float(metadata["format"]["duration"])
    kinds = {stream["codec_type"] for stream in metadata["streams"]}
    if not {"audio", "video"}.issubset(kinds):
        raise ValueError("Smoke file must contain both video and audio")
    if not max(1, expected_duration - 3) <= duration <= expected_duration + 5:
        raise ValueError(f"Smoke duration outside tolerance: {duration}")
    decode = await run_bounded_subprocess(
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-f",
        "null",
        "-",
        timeout_sec=60,
    )
    if decode.returncode:
        raise ValueError("Smoke audio/video cannot be decoded")
    volume = await run_bounded_subprocess(
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(path),
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
        timeout_sec=30,
        stderr_limit=16384,
    )
    match = re.search(r"mean_volume: ([\-\d.]+) dB", volume.stderr)
    if volume.returncode or not match or float(match[1]) < -55:
        raise ValueError("Smoke audio is silent or unavailable")
    frames = []
    for timestamp in (max(0.5, duration * 0.25), duration * 0.75):
        result = await run_bounded_subprocess(
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(timestamp),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=64:36",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "pipe:1",
            timeout_sec=15,
            stdout_limit=64 * 36,
        )
        if result.returncode or len(result.stdout) != 64 * 36:
            raise ValueError("Smoke video frame unavailable")
        frames.append(result.stdout)
    motion = sum(abs(a - b) for a, b in zip(*frames, strict=True)) / (64 * 36 * 255)
    if motion < 0.005:
        raise ValueError("Smoke video is frozen")
    return {
        "duration_sec": duration,
        "streams": sorted(kinds),
        "mean_volume_db": float(match[1]),
        "frame_difference": motion,
    }
