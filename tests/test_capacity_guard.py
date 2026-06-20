from types import SimpleNamespace

import pytest

from recording.capacity_guard import RecordingCapacityError, RecordingCapacityGuard
from recording.worker import RecordingJob


def _job(job_id: str, tmp_path, *, duration_sec: int = 7200) -> RecordingJob:
    return RecordingJob(
        job_id=job_id,
        provider="jitsi",
        meeting_code="room",
        display_name="Recorder Bot",
        duration_sec=duration_sec,
        output_dir=tmp_path / job_id,
        resolution_w=1920,
        resolution_h=1080,
    )


@pytest.mark.asyncio
async def test_capacity_guard_reserves_estimated_disk_space(tmp_path):
    """Concurrent admission should account for already reserved recording output."""
    guard = RecordingCapacityGuard(
        settings_provider=lambda: SimpleNamespace(min_free_disk_gb_before_recording=10.0),
        disk_usage=lambda _path: SimpleNamespace(free=16 * 1024**3),
    )

    first = await guard.reserve(_job("job-a", tmp_path))

    assert first.reserved_gb == pytest.approx(5.0)
    assert guard.reserved_gb() == pytest.approx(5.0)

    with pytest.raises(RecordingCapacityError, match="Insufficient disk space"):
        await guard.reserve(_job("job-b", tmp_path))

    await guard.release("job-a")
    second = await guard.reserve(_job("job-b", tmp_path))

    assert second.reserved_gb == pytest.approx(5.0)
