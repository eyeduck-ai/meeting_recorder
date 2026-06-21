"""Tests for safe MP4 remux/transcode output handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import recording.mp4_validation as mp4_validation_module
import recording.remux as remux_module
import recording.transcode as transcode_module
from recording.subprocess_utils import BoundedSubprocessResult


class EmptyAsyncStream:
    async def readline(self):
        return b""

    async def read(self, _size):
        return b""


class FakeTranscodeProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = EmptyAsyncStream()
        self.stderr = EmptyAsyncStream()

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_remux_publishes_only_validated_temp_output(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mkv"
    output_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mkv")

    async def fake_run_bounded_subprocess(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"mp4")
        return BoundedSubprocessResult(returncode=0, stdout=b"", stderr="")

    monkeypatch.setattr(remux_module, "run_bounded_subprocess", fake_run_bounded_subprocess)
    monkeypatch.setattr(mp4_validation_module, "validate_mp4_file", AsyncMock(return_value=True))

    result = await remux_module.remux_to_mp4(input_path, output_path)

    assert result == output_path
    assert output_path.read_bytes() == b"mp4"
    assert list(tmp_path.glob("*.tmp.*.mp4")) == []


@pytest.mark.asyncio
async def test_remux_discards_invalid_temp_output(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mkv"
    output_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mkv")

    async def fake_run_bounded_subprocess(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"partial")
        return BoundedSubprocessResult(returncode=0, stdout=b"", stderr="")

    monkeypatch.setattr(remux_module, "run_bounded_subprocess", fake_run_bounded_subprocess)
    monkeypatch.setattr(mp4_validation_module, "validate_mp4_file", AsyncMock(return_value=False))

    result = await remux_module.remux_to_mp4(input_path, output_path)

    assert result is None
    assert input_path.exists()
    assert not output_path.exists()
    assert list(tmp_path.glob("*.tmp.*.mp4")) == []


@pytest.mark.asyncio
async def test_remux_discards_failed_temp_output_and_logs_excerpt(tmp_path, monkeypatch, caplog):
    input_path = tmp_path / "recording.mkv"
    output_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mkv")

    async def fake_run_bounded_subprocess(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"partial")
        return BoundedSubprocessResult(returncode=1, stdout=b"", stderr="x" * 5000)

    monkeypatch.setattr(remux_module, "run_bounded_subprocess", fake_run_bounded_subprocess)

    with caplog.at_level("ERROR"):
        result = await remux_module.remux_to_mp4(input_path, output_path)

    assert result is None
    assert input_path.exists()
    assert not output_path.exists()
    assert list(tmp_path.glob("*.tmp.*.mp4")) == []
    assert "Remux failed" in caplog.text


@pytest.mark.asyncio
async def test_transcode_discards_failed_temp_output(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mkv"
    output_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mkv")

    async def fake_exec(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"partial")
        return FakeTranscodeProcess(1)

    monkeypatch.setattr(transcode_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(transcode_module, "_probe_duration_sec", AsyncMock(return_value=60.0))

    result = await transcode_module.transcode_to_mp4(
        input_path=input_path,
        output_path=output_path,
        preset="veryfast",
        crf=23,
        audio_bitrate="128k",
    )

    assert result is None
    assert input_path.exists()
    assert not output_path.exists()
    assert list(tmp_path.glob("*.tmp.*.mp4")) == []


@pytest.mark.asyncio
async def test_transcode_duration_probe_failure_keeps_progress_total_unknown(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mkv"
    output_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mkv")
    progress_updates = []

    async def fake_run_bounded_subprocess(*args, **kwargs):
        assert args[0] == "ffprobe"
        assert kwargs["timeout_sec"] == 10.0
        return BoundedSubprocessResult(returncode=1, stdout=b"", stderr="duration unavailable")

    async def fake_exec(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"mp4")
        return FakeTranscodeProcess(0)

    monkeypatch.setattr(transcode_module, "run_bounded_subprocess", fake_run_bounded_subprocess)
    monkeypatch.setattr(transcode_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(transcode_module, "replace_with_validated_mp4", AsyncMock(return_value=output_path))

    result = await transcode_module.transcode_to_mp4(
        input_path=input_path,
        output_path=output_path,
        preset="veryfast",
        crf=23,
        audio_bitrate="128k",
        progress_callback=lambda current, total: progress_updates.append((current, total)),
    )

    assert result == output_path
    assert progress_updates[0] == (0, None)


@pytest.mark.asyncio
async def test_canonical_mp4_uses_remux_even_when_upload_transcode_enabled(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mkv"
    output_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mkv")

    remux = AsyncMock(return_value=output_path)
    transcode = AsyncMock(return_value=output_path)
    monkeypatch.setattr(remux_module, "remux_to_mp4", remux)
    monkeypatch.setattr(remux_module, "transcode_to_mp4", transcode)

    result = await remux_module.ensure_canonical_mp4(input_path)

    assert result == output_path
    remux.assert_awaited_once()
    transcode.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_mp4_uses_transcode_when_enabled(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mkv"
    local_path = tmp_path / "recording.mp4"
    upload_path = tmp_path / "recording.upload.mp4"
    input_path.write_bytes(b"mkv")

    settings = SimpleNamespace(
        ffmpeg_transcode_on_upload=True,
        ffmpeg_transcode_preset="veryfast",
        ffmpeg_transcode_crf=23,
        ffmpeg_transcode_audio_bitrate="128k",
        ffmpeg_transcode_video_bitrate=None,
    )
    transcode = AsyncMock(return_value=upload_path)
    remux = AsyncMock(return_value=local_path)
    monkeypatch.setattr(remux_module, "get_settings", lambda: settings)
    monkeypatch.setattr(remux_module, "transcode_to_mp4", transcode)
    monkeypatch.setattr(remux_module, "remux_to_mp4", remux)

    result = await remux_module.ensure_upload_mp4(input_path)

    assert result == upload_path
    remux.assert_awaited_once()
    transcode.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_mp4_transcodes_existing_canonical_mp4_when_enabled(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mp4"
    upload_path = tmp_path / "recording.upload.mp4"
    input_path.write_bytes(b"mp4")

    settings = SimpleNamespace(
        ffmpeg_transcode_on_upload=True,
        ffmpeg_transcode_preset="veryfast",
        ffmpeg_transcode_crf=23,
        ffmpeg_transcode_audio_bitrate="128k",
        ffmpeg_transcode_video_bitrate=None,
    )
    transcode = AsyncMock(return_value=upload_path)
    remux = AsyncMock()
    monkeypatch.setattr(remux_module, "get_settings", lambda: settings)
    monkeypatch.setattr(remux_module, "transcode_to_mp4", transcode)
    monkeypatch.setattr(remux_module, "remux_to_mp4", remux)

    result = await remux_module.ensure_upload_mp4(input_path)

    assert result == upload_path
    transcode.assert_awaited_once()
    remux.assert_not_awaited()


@pytest.mark.asyncio
async def test_mp4_validation_failure_returns_false(tmp_path, monkeypatch):
    input_path = tmp_path / "recording.mp4"
    input_path.write_bytes(b"mp4")

    async def fake_run_bounded_subprocess(*args, **kwargs):
        return BoundedSubprocessResult(returncode=124, stdout=b"", stderr="process timed out", timed_out=True)

    monkeypatch.setattr(mp4_validation_module, "run_bounded_subprocess", fake_run_bounded_subprocess)

    assert await mp4_validation_module.validate_mp4_file(input_path) is False
