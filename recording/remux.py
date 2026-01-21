"""Helpers for remuxing recordings."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config.settings import get_settings
from recording.transcode import transcode_to_mp4

logger = logging.getLogger(__name__)


def derive_mp4_path(input_path: Path) -> Path:
    """Derive MP4 path from input path."""
    return input_path.with_suffix(".mp4")


def pick_preferred_video_path(input_path: Path) -> Path:
    """Pick a preferred playback path without remuxing."""
    if input_path.suffix.lower() == ".mkv":
        mp4_path = derive_mp4_path(input_path)
        if mp4_path.exists():
            return mp4_path
    return input_path


def _is_mp4_fresh(input_path: Path, mp4_path: Path) -> bool:
    try:
        return mp4_path.stat().st_mtime >= input_path.stat().st_mtime
    except OSError:
        return False


async def ensure_mp4(
    input_path: Path,
    remux_log_path: Path | None = None,
    transcode_log_path: Path | None = None,
    progress_callback=None,
) -> Path | None:
    """Ensure an MP4 exists for upload and return its path."""
    if input_path.suffix.lower() == ".mp4":
        return input_path

    settings = get_settings()
    mp4_path = derive_mp4_path(input_path)

    if settings.ffmpeg_transcode_on_upload:
        if mp4_path.exists() and _is_mp4_fresh(input_path, mp4_path):
            return mp4_path
        return await transcode_to_mp4(
            input_path=input_path,
            output_path=mp4_path,
            preset=settings.ffmpeg_transcode_preset,
            crf=settings.ffmpeg_transcode_crf,
            audio_bitrate=settings.ffmpeg_transcode_audio_bitrate,
            log_path=transcode_log_path or remux_log_path,
            progress_callback=progress_callback,
        )

    if mp4_path.exists() and _is_mp4_fresh(input_path, mp4_path):
        return mp4_path

    return await remux_to_mp4(input_path, mp4_path, remux_log_path)


async def remux_to_mp4(input_path: Path, output_path: Path, log_path: Path | None = None) -> Path | None:
    """Remux a recording into MP4 without re-encoding."""
    if not input_path.exists():
        logger.warning(f"Remux skipped, input not found: {input_path}")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    stderr_text = stderr.decode(errors="ignore")
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(stderr_text, encoding="utf-8")

    if process.returncode != 0 or not output_path.exists():
        logger.error(f"Remux failed: {stderr_text[:400]}")
        return None

    logger.info(f"Remuxed MP4 created: {output_path}")
    return output_path
