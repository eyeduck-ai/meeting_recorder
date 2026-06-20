"""Process-local recording capacity guards."""

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import get_settings


class RecordingCapacityError(RuntimeError):
    """Raised when a recording cannot be admitted because capacity is exhausted."""


@dataclass(frozen=True)
class RecordingCapacityReservation:
    """Disk reservation held by one active recording job."""

    job_id: str
    reserved_gb: float


class RecordingCapacityGuard:
    """Reserve estimated disk space for active recording jobs in this process."""

    def __init__(
        self,
        *,
        settings_provider: Callable = get_settings,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self._settings_provider = settings_provider
        self._disk_usage = disk_usage
        self._reservations: dict[str, RecordingCapacityReservation] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, job) -> RecordingCapacityReservation:
        """Reserve estimated disk space for a job before runtime startup."""
        async with self._lock:
            existing = self._reservations.get(job.job_id)
            if existing:
                return existing

            settings = self._settings_provider()
            min_free_gb = float(getattr(settings, "min_free_disk_gb_before_recording", 0) or 0)
            estimated_gb = self.estimate_required_gb(job)

            if min_free_gb > 0:
                root = self._disk_root(job.output_dir)
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    free_gb = self._disk_usage(root).free / (1024**3)
                except OSError as e:
                    raise RecordingCapacityError(f"Failed to inspect disk space before recording: {e}") from e

                reserved_gb = sum(reservation.reserved_gb for reservation in self._reservations.values())
                if free_gb - reserved_gb - estimated_gb < min_free_gb:
                    raise RecordingCapacityError(
                        "Insufficient disk space: "
                        f"{free_gb:.1f} GB free, {reserved_gb:.1f} GB reserved, "
                        f"{estimated_gb:.1f} GB required, keep {min_free_gb:.1f} GB free"
                    )

            reservation = RecordingCapacityReservation(job_id=job.job_id, reserved_gb=estimated_gb)
            self._reservations[job.job_id] = reservation
            return reservation

    async def release(self, job_id: str) -> None:
        """Release a job's process-local disk reservation."""
        async with self._lock:
            self._reservations.pop(job_id, None)

    def reserved_gb(self) -> float:
        """Return currently reserved GB for tests and diagnostics."""
        return sum(reservation.reserved_gb for reservation in self._reservations.values())

    def estimate_required_gb(self, job) -> float:
        """Estimate recording output size in GB using conservative v1 defaults."""
        width = max(1, int(getattr(job, "resolution_w", 1920) or 1920))
        height = max(1, int(getattr(job, "resolution_h", 1080) or 1080))
        duration_sec = max(1, int(getattr(job, "duration_sec", 3600) or 3600))
        pixels = width * height
        p720 = 1280 * 720
        p1080 = 1920 * 1080

        if pixels <= p720:
            gb_per_hour = 1.2 * (pixels / p720)
        else:
            gb_per_hour = 2.5 * (pixels / p1080)

        return max(1.0, gb_per_hour * (duration_sec / 3600))

    def _disk_root(self, output_dir: Path) -> Path:
        return output_dir if output_dir.exists() else output_dir.parent
