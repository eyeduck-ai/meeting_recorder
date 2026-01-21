from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

Phase = Literal["compressing", "uploading"]


@dataclass(frozen=True)
class ProgressInfo:
    phase: Phase
    percent: float | None
    current: int | None
    total: int | None
    unit: Literal["ms", "bytes"] | None
    updated_at: datetime


_progress: dict[str, ProgressInfo] = {}


def update_progress(job_id: str, phase: Phase, current: int | None, total: int | None, unit: Literal["ms", "bytes"]):
    percent = None
    if current is not None and total:
        percent = max(0.0, min(100.0, (current / total) * 100))
    _progress[job_id] = ProgressInfo(
        phase=phase,
        percent=percent,
        current=current,
        total=total,
        unit=unit,
        updated_at=datetime.now(UTC),
    )


def clear_progress(job_id: str) -> None:
    _progress.pop(job_id, None)


def get_progress(job_id: str) -> ProgressInfo | None:
    return _progress.get(job_id)


def get_latest_progress() -> tuple[str, ProgressInfo] | None:
    if not _progress:
        return None
    job_id, info = max(_progress.items(), key=lambda item: item[1].updated_at)
    return job_id, info
