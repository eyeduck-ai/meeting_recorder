"""Process-local allocation of per-recording runtime resources."""

import asyncio
import re
from dataclasses import dataclass

from config.settings import get_settings


@dataclass(frozen=True)
class RuntimeResourceLease:
    """Display and audio resources assigned to one recording job."""

    job_id: str
    display_num: int
    pulse_sink_name: str

    @property
    def display(self) -> str:
        return f":{self.display_num}"

    @property
    def pulse_monitor(self) -> str:
        return f"{self.pulse_sink_name}.monitor"


class RuntimeResourceAllocator:
    """Allocate unique X displays and Pulse sinks within this app process."""

    def __init__(
        self,
        *,
        display_start: int | None = None,
        display_pool_size: int | None = None,
    ) -> None:
        settings = get_settings()
        start = display_start if display_start is not None else settings.recording_display_start
        pool_size = display_pool_size if display_pool_size is not None else settings.recording_display_pool_size
        pool_size = max(1, int(pool_size))
        self._available_displays = list(range(int(start), int(start) + pool_size))
        self._leases: dict[str, RuntimeResourceLease] = {}
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return len(self._available_displays) + len(self._leases)

    async def acquire(self, job_id: str) -> RuntimeResourceLease:
        """Assign a display and sink to job_id."""
        async with self._lock:
            existing = self._leases.get(job_id)
            if existing:
                return existing
            if not self._available_displays:
                raise RuntimeError("No recording display is available")

            display_num = self._available_displays.pop(0)
            lease = RuntimeResourceLease(
                job_id=job_id,
                display_num=display_num,
                pulse_sink_name=f"mr_sink_{self._sanitize_job_id(job_id)}",
            )
            self._leases[job_id] = lease
            return lease

    async def release(self, job_id: str) -> None:
        """Release resources held by job_id."""
        async with self._lock:
            lease = self._leases.pop(job_id, None)
            if not lease:
                return
            self._available_displays.append(lease.display_num)
            self._available_displays.sort()

    def _sanitize_job_id(self, job_id: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_]", "_", str(job_id))
        return value[:40] or "job"
