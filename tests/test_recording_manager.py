"""Tests for recording manager filesystem behavior."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.recording_manager as recording_manager_module
from services.recording_manager import RecordingManager, get_recording_manager


def test_get_recording_manager_uses_settings_recordings_dir(monkeypatch, tmp_path):
    configured_dir = tmp_path / "configured-recordings"
    monkeypatch.setattr(recording_manager_module, "_recording_manager", None)
    monkeypatch.setattr(
        recording_manager_module,
        "get_settings",
        lambda: SimpleNamespace(recordings_dir=configured_dir),
    )

    manager = get_recording_manager()

    assert manager.recordings_dir == configured_dir


def test_list_recordings_includes_root_and_job_subdirectories(tmp_path):
    root_video = tmp_path / "root.mp4"
    nested_dir = tmp_path / "20260611_job123"
    nested_dir.mkdir()
    nested_video = nested_dir / "recording_job123.mkv"
    root_video.write_bytes(b"root-video")
    nested_video.write_bytes(b"nested-video")

    recordings = RecordingManager(tmp_path).list_recordings()
    paths = {recording["path"] for recording in recordings}
    relative_paths = {recording["relative_path"] for recording in recordings}

    assert str(root_video) in paths
    assert str(nested_video) in paths
    assert "root.mp4" in relative_paths
    assert str(nested_video.relative_to(tmp_path)) in relative_paths


def test_list_recordings_excludes_thumbnails_and_paginates_after_sort(tmp_path):
    thumbnails_dir = tmp_path / "thumbnails"
    thumbnails_dir.mkdir()
    (thumbnails_dir / "ignored.mp4").write_bytes(b"not-a-recording")

    small = tmp_path / "small.mp4"
    medium = tmp_path / "medium.mp4"
    large = tmp_path / "large.mp4"
    small.write_bytes(b"1")
    medium.write_bytes(b"123")
    large.write_bytes(b"12345")

    recordings = RecordingManager(tmp_path).list_recordings(limit=1, offset=1, order_by="largest")

    assert [recording["filename"] for recording in recordings] == ["medium.mp4"]


def test_list_recordings_uses_single_stat_per_video_for_metadata(monkeypatch, tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    stat_calls: dict[str, int] = {}
    original_stat = Path.stat

    def counted_stat(self, *args, **kwargs):
        if self.suffix.lower() in recording_manager_module.VIDEO_EXTENSIONS:
            stat_calls[str(self)] = stat_calls.get(str(self), 0) + 1
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted_stat)

    recordings = RecordingManager(tmp_path).list_recordings(order_by="newest")

    assert len(recordings) == 2
    assert stat_calls == {str(first): 1, str(second): 1}


def test_disk_usage_counts_nested_recordings(tmp_path):
    nested_dir = tmp_path / "20260611_job123"
    nested_dir.mkdir()
    (nested_dir / "recording_job123.mkv").write_bytes(b"12345")
    (tmp_path / "legacy.mp4").write_bytes(b"123")

    usage = RecordingManager(tmp_path).get_disk_usage()

    assert usage["recordings_count"] == 2
    assert usage["recordings_bytes"] == 8


@pytest.mark.asyncio
async def test_cleanup_old_recordings_deletes_nested_videos(tmp_path):
    nested_dir = tmp_path / "20260611_job123"
    nested_dir.mkdir()
    nested_video = nested_dir / "recording_job123.mkv"
    nested_video.write_bytes(b"old-video")
    old_timestamp = time.time() - 40 * 24 * 60 * 60
    os.utime(nested_video, (old_timestamp, old_timestamp))

    result = await RecordingManager(tmp_path).cleanup_old_recordings(max_age_days=30)

    assert result["deleted_count"] == 1
    assert not nested_video.exists()
