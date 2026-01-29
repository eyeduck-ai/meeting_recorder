from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _probe_duration_sec(input_path: Path) -> float | None:
    try:
        result = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        duration_str = stdout.decode().strip()
        return float(duration_str) if duration_str else None
    except Exception as exc:
        logger.warning(f"Could not probe duration for {input_path}: {exc}")
        return None


async def transcode_to_mp4(
    input_path: Path,
    output_path: Path,
    preset: str,
    crf: int,
    audio_bitrate: str,
    video_bitrate: str | None = None,
    log_path: Path | None = None,
    progress_callback=None,
) -> Path | None:
    """Transcode a recording into a smaller MP4 with re-encoding."""
    if not input_path.exists():
        logger.warning(f"Transcode skipped, input not found: {input_path}")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)

    duration_sec = await _probe_duration_sec(input_path)
    total_ms = int(duration_sec * 1000) if duration_sec else None
    if progress_callback:
        progress_callback(0, total_ms)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
    ]

    # Add video bitrate limit if specified
    if video_bitrate:
        cmd += ["-maxrate", video_bitrate, "-bufsize", video_bitrate]

    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _read_progress():
        if not process.stdout:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="ignore").strip()
            if "=" not in text:
                continue
            key, value = text.split("=", 1)
            if key == "out_time_ms":
                try:
                    current_ms = int(value)
                except ValueError:
                    continue
                if progress_callback:
                    progress_callback(current_ms, total_ms)
            elif key == "progress" and value == "end":
                if progress_callback and total_ms:
                    progress_callback(total_ms, total_ms)

    async def _read_stderr():
        if not process.stderr:
            return
        writer = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            writer = log_path.open("w", encoding="utf-8")
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                if writer:
                    writer.write(chunk.decode(errors="ignore"))
        finally:
            if writer:
                writer.close()

    progress_task = asyncio.create_task(_read_progress())
    stderr_task = asyncio.create_task(_read_stderr())

    returncode = await process.wait()
    await progress_task
    await stderr_task

    if returncode != 0 or not output_path.exists():
        logger.error(f"Transcode failed with return code {returncode}")
        return None

    logger.info(f"Transcoded MP4 created: {output_path}")
    return output_path
