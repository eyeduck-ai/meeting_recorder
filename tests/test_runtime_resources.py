import pytest

from recording.runtime_resources import RuntimeResourceAllocator


@pytest.mark.asyncio
async def test_runtime_resource_allocator_assigns_unique_displays_and_reuses_released():
    allocator = RuntimeResourceAllocator(display_start=200, display_pool_size=2)

    first = await allocator.acquire("job-a")
    second = await allocator.acquire("job-b")

    assert first.display == ":200"
    assert second.display == ":201"
    assert first.pulse_sink_name == "mr_sink_job_a"
    assert second.pulse_monitor == "mr_sink_job_b.monitor"

    await allocator.release("job-a")
    third = await allocator.acquire("job-c")

    assert third.display == ":200"


def test_runtime_resource_allocator_does_not_keep_unused_display_probe():
    assert not hasattr(RuntimeResourceAllocator, "is_allocated")


@pytest.mark.asyncio
async def test_runtime_resource_allocator_raises_when_pool_is_exhausted():
    allocator = RuntimeResourceAllocator(display_start=200, display_pool_size=1)

    await allocator.acquire("job-a")

    with pytest.raises(RuntimeError, match="No recording display"):
        await allocator.acquire("job-b")
