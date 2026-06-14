"""Tests for scheduling upload orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import scheduling.upload_runner as upload_runner_module
from scheduling.upload_runner import UploadRequest, YouTubeUploadRunner


@pytest.mark.asyncio
async def test_run_upload_task_remuxes_mkv_and_delegates_youtube_upload(tmp_path, monkeypatch):
    video_path = tmp_path / "recording.mkv"
    upload_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"mkv")
    upload_path.write_bytes(b"mp4")

    settings = SimpleNamespace(diagnostics_dir=tmp_path / "diagnostics")
    ensure_mp4 = AsyncMock(return_value=upload_path)
    clear_progress = Mock()
    update_progress = Mock()

    monkeypatch.setattr(upload_runner_module, "get_settings", lambda: settings)
    monkeypatch.setattr(upload_runner_module, "ensure_mp4", ensure_mp4)
    monkeypatch.setattr(upload_runner_module, "clear_progress", clear_progress)
    monkeypatch.setattr(upload_runner_module, "update_progress", update_progress)

    runner = YouTubeUploadRunner()
    runner.upload_to_youtube = AsyncMock()

    await runner.run_upload_task(
        UploadRequest(
            job_id="job123",
            video_path=video_path,
            title="Meeting",
            privacy="unlisted",
        )
    )

    ensure_mp4.assert_awaited_once()
    ensure_kwargs = ensure_mp4.await_args.kwargs
    assert ensure_kwargs["remux_log_path"] == settings.diagnostics_dir / "job123" / "remux.log"
    assert ensure_kwargs["transcode_log_path"] == settings.diagnostics_dir / "job123" / "transcode.log"

    ensure_kwargs["progress_callback"](100, 200)
    update_progress.assert_called_with("job123", "compressing", 100, 200, "ms")
    runner.upload_to_youtube.assert_awaited_once_with(
        job_id="job123",
        video_path=upload_path,
        title="Meeting",
        privacy="unlisted",
    )
    clear_progress.assert_called_once_with("job123")
