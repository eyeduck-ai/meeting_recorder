"""Helpers for remuxing recordings."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from config.settings import get_settings
from recording.mp4_validation import discard_file, replace_with_validated_mp4, temporary_mp4_path, validate_mp4_file
from recording.subprocess_utils import run_bounded_subprocess
from recording.transcode import transcode_to_mp4

logger = logging.getLogger(__name__)


def derive_mp4_path(input_path: Path) -> Path:
    """Derive MP4 path from input path."""
    return input_path.with_suffix(".mp4")


def recording_file_variants(path: Path) -> tuple[Path, ...]:
    """Return compatible local recording file variants for an MKV/MP4 artifact."""
    variants = {path}
    if path.suffix.lower() == ".mkv":
        variants.add(path.with_suffix(".mp4"))
    elif path.suffix.lower() == ".mp4":
        variants.add(path.with_suffix(".mkv"))
    return tuple(sorted(variants))


def delete_recording_artifacts(
    candidates: Iterable[Path | None],
    *,
    preserve_path: Path | None = None,
) -> tuple[tuple[Path, ...], tuple[tuple[Path, OSError], ...]]:
    """Best-effort delete recording artifacts while preserving optional raw MKV/MP4 variants."""
    deleted: list[Path] = []
    errors: list[tuple[Path, OSError]] = []
    seen: set[Path] = set()
    preserved = set(recording_file_variants(preserve_path)) if preserve_path is not None else set()

    for candidate in candidates:
        if candidate is None:
            continue
        for artifact in recording_file_variants(candidate):
            if artifact in seen:
                continue
            seen.add(artifact)
            if artifact in preserved:
                continue
            try:
                if artifact.exists():
                    artifact.unlink()
                    deleted.append(artifact)
            except OSError as exc:
                errors.append((artifact, exc))

    return tuple(deleted), tuple(errors)


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


async def ensure_canonical_mp4(
    input_path: Path,
    remux_log_path: Path | None = None,
) -> Path | None:
    """Ensure a local canonical MP4 exists using a fast remux only."""
    if input_path.suffix.lower() == ".mp4":
        return input_path if await validate_mp4_file(input_path) else None

    mp4_path = derive_mp4_path(input_path)
    if mp4_path.exists() and _is_mp4_fresh(input_path, mp4_path) and await validate_mp4_file(mp4_path):
        return mp4_path

    return await remux_to_mp4(input_path, mp4_path, remux_log_path)


async def ensure_upload_mp4(
    input_path: Path,
    remux_log_path: Path | None = None,
    transcode_log_path: Path | None = None,
    progress_callback=None,
) -> Path | None:
    """Ensure an MP4 exists for upload and return its path."""
    settings = get_settings()

    source_path = input_path
    if source_path.suffix.lower() != ".mp4":
        source_path = await ensure_canonical_mp4(source_path, remux_log_path=remux_log_path)
        if not source_path:
            return None

    if not settings.ffmpeg_transcode_on_upload:
        return source_path if await validate_mp4_file(source_path) else None

    upload_path = source_path.with_name(f"{source_path.stem}.upload.mp4")
    return await transcode_to_mp4(
        input_path=source_path,
        output_path=upload_path,
        preset=settings.ffmpeg_transcode_preset,
        crf=settings.ffmpeg_transcode_crf,
        audio_bitrate=settings.ffmpeg_transcode_audio_bitrate,
        video_bitrate=settings.ffmpeg_transcode_video_bitrate,
        log_path=transcode_log_path or remux_log_path,
        progress_callback=progress_callback,
    )


async def remux_to_mp4(input_path: Path, output_path: Path, log_path: Path | None = None) -> Path | None:
    """Remux a recording into MP4 without re-encoding."""
    if not input_path.exists():
        logger.warning(f"Remux skipped, input not found: {input_path}")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = temporary_mp4_path(output_path)

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
        str(temp_path),
    ]

    try:
        result = await run_bounded_subprocess(
            *cmd,
            timeout_sec=3600,
            stderr_log_path=log_path,
            stdout_limit=1024,
            stderr_limit=4096,
        )
    except Exception:
        discard_file(temp_path)
        raise

    if result.returncode != 0 or not temp_path.exists():
        discard_file(temp_path)
        logger.error(f"Remux failed: {result.stderr[:400]}")
        return None

    published_path = await replace_with_validated_mp4(temp_path, output_path)
    if not published_path:
        logger.error("Remux output failed MP4 validation: %s", output_path)
        return None

    logger.info(f"Remuxed MP4 created: {output_path}")
    return published_path
