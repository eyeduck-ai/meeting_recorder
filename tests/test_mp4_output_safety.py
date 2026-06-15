"""Tests for safe MP4 remux/transcode output handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import recording.mp4_validation as mp4_validation_module
import recording.remux as remux_module
import recording.transcode as transcode_module


class FakeRemuxProcess:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


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

    async def fake_exec(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"mp4")
        return FakeRemuxProcess(0)

    monkeypatch.setattr(remux_module.asyncio, "create_subprocess_exec", fake_exec)
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

    async def fake_exec(*args, **kwargs):
        temp_path = Path(args[-1])
        temp_path.write_bytes(b"partial")
        return FakeRemuxProcess(0)

    monkeypatch.setattr(remux_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(mp4_validation_module, "validate_mp4_file", AsyncMock(return_value=False))

    result = await remux_module.remux_to_mp4(input_path, output_path)

    assert result is None
    assert input_path.exists()
    assert not output_path.exists()
    assert list(tmp_path.glob("*.tmp.*.mp4")) == []


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
