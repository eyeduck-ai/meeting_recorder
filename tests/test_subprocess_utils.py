import asyncio
import os
import sys

import pytest

from recording import subprocess_utils


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeProcess:
    def __init__(self, *, returncode=None, wait_side_effect=None, stdout_chunks=None, stderr_chunks=None):
        self.returncode = returncode
        self.stdout = FakeStream(stdout_chunks or [])
        self.stderr = FakeStream(stderr_chunks or [])
        self.terminated = False
        self.killed = False
        self._wait_side_effect = wait_side_effect

    async def wait(self):
        if self._wait_side_effect:
            return await self._wait_side_effect(self)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_bounded_subprocess_limits_stderr_excerpt_and_writes_full_log(tmp_path, monkeypatch):
    log_path = tmp_path / "stderr.log"
    process = FakeProcess(returncode=1, stdout_chunks=[b"stdout"], stderr_chunks=[b"abcdef"])

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(subprocess_utils.asyncio, "create_subprocess_exec", fake_exec)

    result = await subprocess_utils.run_bounded_subprocess(
        "ffmpeg",
        timeout_sec=10,
        stderr_limit=3,
        stderr_log_path=log_path,
    )

    assert result.returncode == 1
    assert result.stdout == b"stdout"
    assert result.stderr == "abc"
    assert log_path.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_bounded_subprocess_timeout_terminates_then_kills(monkeypatch):
    async def wait_until_killed(process):
        if process.killed:
            return -9
        raise TimeoutError

    process = FakeProcess(returncode=None, wait_side_effect=wait_until_killed)

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(subprocess_utils.asyncio, "create_subprocess_exec", fake_exec)

    result = await subprocess_utils.run_bounded_subprocess("ffmpeg", timeout_sec=0.01)

    assert result.returncode == subprocess_utils.SUBPROCESS_TIMEOUT_RETURN_CODE
    assert result.timed_out is True
    assert process.terminated is True
    assert process.killed is True
    assert "process timed out" in result.stderr


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX termination and process existence checks")
async def test_canceling_real_subprocess_kills_child_and_drains_reader_tasks(tmp_path):
    pid_file = tmp_path / "child.pid"
    stderr_log = tmp_path / "child.stderr"
    script = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); print('diagnostic', file=sys.stderr, flush=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(
        subprocess_utils.run_bounded_subprocess(
            sys.executable,
            "-c",
            script,
            str(pid_file),
            timeout_sec=60,
            stderr_log_path=stderr_log,
            terminate_timeout_sec=0.05,
        )
    )
    try:
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert stderr_log.read_text() == "diagnostic\n"
        leaked = [pending for pending in asyncio.all_tasks() - before if not pending.done()]
        assert leaked == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_subprocess_stream_log_error_still_reaps_child(tmp_path):
    invalid_log = tmp_path / "directory-as-log"
    invalid_log.mkdir()
    before = set(asyncio.all_tasks())
    with pytest.raises(IsADirectoryError):
        await asyncio.wait_for(
            subprocess_utils.run_bounded_subprocess(
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                timeout_sec=60,
                stderr_log_path=invalid_log,
                terminate_timeout_sec=0.05,
            ),
            timeout=1,
        )
    assert [pending for pending in asyncio.all_tasks() - before if not pending.done()] == []
