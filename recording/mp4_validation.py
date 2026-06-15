"""MP4 validation and safe output helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


def temporary_mp4_path(output_path: Path) -> Path:
    """Return a same-directory temporary MP4 path for atomic replacement."""
    return output_path.with_name(f"{output_path.stem}.tmp.{uuid4().hex}.mp4")


def discard_file(path: Path) -> None:
    """Best-effort removal for partial derived video files."""
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("Failed to remove partial MP4 %s: %s", path, exc)


async def validate_mp4_file(path: Path) -> bool:
    """Validate that an MP4 is readable and has plausible video metadata."""
    path = Path(path)
    try:
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            return False
    except OSError:
        return False

    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration:stream=codec_type",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except Exception as exc:
        logger.warning("Could not validate MP4 %s with ffprobe: %s", path, exc)
        return False

    if process.returncode != 0:
        logger.warning("MP4 validation failed for %s: %s", path, stderr.decode(errors="ignore")[:400])
        return False

    try:
        metadata = json.loads(stdout.decode(errors="ignore") or "{}")
    except json.JSONDecodeError:
        return False

    streams = metadata.get("streams") or []
    has_video = any(stream.get("codec_type") == "video" for stream in streams if isinstance(stream, dict))
    try:
        duration = float((metadata.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0

    return has_video and duration > 0


async def replace_with_validated_mp4(temp_path: Path, output_path: Path) -> Path | None:
    """Atomically publish a temporary MP4 only after validation succeeds."""
    if not await validate_mp4_file(temp_path):
        discard_file(temp_path)
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(output_path)
    return output_path
