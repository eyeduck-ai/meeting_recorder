"""Bounded subprocess helpers for media tools."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

SUBPROCESS_TIMEOUT_RETURN_CODE = 124


@dataclass(frozen=True)
class BoundedSubprocessResult:
    """Result from a bounded subprocess execution."""

    returncode: int
    stdout: bytes
    stderr: str
    timed_out: bool = False


async def _read_stream(
    stream: asyncio.StreamReader | None,
    *,
    limit: int,
    log_path: Path | None = None,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    log_file = None
    try:
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("wb")
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            if log_file:
                log_file.write(chunk)
            if total < limit:
                take = chunk[: limit - total]
                chunks.append(take)
                total += len(take)
    finally:
        if log_file:
            log_file.close()
    return b"".join(chunks)


async def _terminate_process(process: asyncio.subprocess.Process, *, terminate_timeout_sec: float) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=terminate_timeout_sec)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def run_bounded_subprocess(
    *cmd: str,
    timeout_sec: float,
    stdout_limit: int = 4096,
    stderr_limit: int = 4096,
    stderr_log_path: Path | None = None,
    terminate_timeout_sec: float = 2.0,
) -> BoundedSubprocessResult:
    """Run a subprocess with timeout and bounded output excerpts."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_task = asyncio.create_task(_read_stream(process.stdout, limit=stdout_limit))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, limit=stderr_limit, log_path=stderr_log_path))
    timed_out = False
    try:
        async with asyncio.timeout(timeout_sec):
            returncode = await process.wait()
    except TimeoutError:
        timed_out = True
        await _terminate_process(process, terminate_timeout_sec=terminate_timeout_sec)
        returncode = SUBPROCESS_TIMEOUT_RETURN_CODE

    stdout = await stdout_task
    stderr_bytes = await stderr_task
    stderr = stderr_bytes.decode(errors="ignore")
    if timed_out:
        stderr = (stderr + "\nprocess timed out").strip()
    return BoundedSubprocessResult(
        returncode=returncode or 0,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )
