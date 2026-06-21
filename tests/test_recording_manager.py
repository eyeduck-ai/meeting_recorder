import pytest

import services.recording_manager as recording_manager_module
from recording.subprocess_utils import BoundedSubprocessResult


@pytest.mark.asyncio
async def test_generate_thumbnail_failure_returns_none(tmp_path, monkeypatch):
    video_path = tmp_path / "recording.mp4"
    output_path = tmp_path / "thumb.jpg"
    video_path.write_bytes(b"video")

    async def fake_run_bounded_subprocess(*args, **kwargs):
        return BoundedSubprocessResult(returncode=124, stdout=b"", stderr="process timed out", timed_out=True)

    monkeypatch.setattr(recording_manager_module, "run_bounded_subprocess", fake_run_bounded_subprocess)

    manager = recording_manager_module.RecordingManager(tmp_path)

    assert await manager.generate_thumbnail(video_path, output_path=output_path) is None
    assert not output_path.exists()
