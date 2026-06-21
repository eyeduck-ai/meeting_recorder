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
