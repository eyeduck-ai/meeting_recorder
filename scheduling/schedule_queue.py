"""In-memory FIFO queue state for recording runs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from utils.timezone import utc_now

QueuedRunKind = Literal["schedule", "immediate"]


@dataclass(frozen=True)
class QueueScheduleResult:
    """Result of attempting to enqueue a schedule recording."""

    accepted: bool
    status: str
    schedule_id: int
    queue_position: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class QueueImmediateResult:
    """Result of attempting to enqueue an immediate recording."""

    accepted: bool
    status: str
    job_id: str
    queue_position: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class QueuedRunItem:
    """A queued run request for either a schedule or an immediate job."""

    kind: QueuedRunKind
    created_at: datetime
    schedule_id: int | None = None
    job_id: str | None = None
    manual_trigger: bool = False


@dataclass(frozen=True)
class QueuedRunView:
    """Stable view of a queued run with its current 1-based position."""

    kind: QueuedRunKind
    queue_position: int
    created_at: datetime
    schedule_id: int | None = None
    job_id: str | None = None
    manual_trigger: bool = False


class ScheduleRunQueue:
    """Track FIFO run queue plus schedule pending/active duplicate state."""

    def __init__(self) -> None:
        self._current_schedule_id: int | None = None
        self._active_schedule_ids: set[int] = set()
        self._queue: list[QueuedRunItem] = []
        self._pending_schedule_ids: set[int] = set()

    @property
    def current_schedule_id(self) -> int | None:
        return self._current_schedule_id

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    @property
    def has_queued(self) -> bool:
        return bool(self._queue)

    def enqueue_schedule(
        self,
        schedule_id: int,
        *,
        manual_trigger: bool = False,
        can_start_now: bool = False,
    ) -> QueueScheduleResult:
        """Add a schedule to the queue unless it is already active or queued."""
        if self.is_schedule_active_or_queued(schedule_id):
            return QueueScheduleResult(
                accepted=False,
                status="duplicate",
                schedule_id=schedule_id,
                queue_position=0,
                reason="Schedule is already running or queued",
            )

        status = "triggered" if can_start_now and not self._queue else "queued"
        queue_position = 0 if status == "triggered" else self.queue_length + 1

        self._queue.append(
            QueuedRunItem(
                kind="schedule",
                created_at=utc_now(),
                schedule_id=schedule_id,
                manual_trigger=manual_trigger,
            )
        )
        self._pending_schedule_ids.add(schedule_id)
        return QueueScheduleResult(
            accepted=True,
            status=status,
            schedule_id=schedule_id,
            queue_position=queue_position,
        )

    def enqueue_immediate(
        self,
        job_id: str,
        *,
        can_start_now: bool = False,
        schedule_id: int | None = None,
    ) -> QueueImmediateResult:
        """Add an immediate job to the FIFO queue."""
        status = "triggered" if can_start_now and not self._queue else "queued"
        queue_position = 0 if status == "triggered" else self.queue_length + 1
        self._queue.append(
            QueuedRunItem(
                kind="immediate",
                created_at=utc_now(),
                schedule_id=schedule_id,
                job_id=job_id,
            )
        )
        if schedule_id is not None:
            self._pending_schedule_ids.add(schedule_id)
        return QueueImmediateResult(
            accepted=True,
            status=status,
            job_id=job_id,
            queue_position=queue_position,
        )

    def pop_next(self) -> QueuedRunItem | None:
        """Move the next queued item into active state when applicable."""
        if not self._queue:
            return None

        item = self._queue.pop(0)
        if item.schedule_id is not None:
            self._pending_schedule_ids.discard(item.schedule_id)
            self._active_schedule_ids.add(item.schedule_id)
            self._current_schedule_id = item.schedule_id
        return item

    def restore_front(self, item: QueuedRunItem) -> None:
        """Restore an item that could not be dispatched."""
        if item.schedule_id is not None:
            self._active_schedule_ids.discard(item.schedule_id)
            self._pending_schedule_ids.add(item.schedule_id)
            if self._current_schedule_id == item.schedule_id:
                self._current_schedule_id = next(iter(self._active_schedule_ids), None)
        self._queue.insert(0, item)

    def mark_done(self, item: QueuedRunItem) -> None:
        """Clear active state for a completed run item."""
        if item.schedule_id is not None:
            self.mark_schedule_done(item.schedule_id)

    def mark_schedule_done(self, schedule_id: int | None = None) -> None:
        """Clear active state for a schedule run."""
        done_id = schedule_id if schedule_id is not None else self._current_schedule_id
        if done_id is not None:
            self._active_schedule_ids.discard(done_id)
        if self._current_schedule_id == done_id:
            self._current_schedule_id = next(iter(self._active_schedule_ids), None)

    def cancel_queued_immediate(self, job_id: str) -> bool:
        """Remove a queued immediate job without affecting active jobs."""
        for idx, item in enumerate(self._queue):
            if item.kind == "immediate" and item.job_id == job_id:
                self._queue.pop(idx)
                if item.schedule_id is not None:
                    self._pending_schedule_ids.discard(item.schedule_id)
                return True
        return False

    def cancel_queued_schedule(self, schedule_id: int) -> bool:
        """Remove a queued schedule run without disabling the schedule."""
        for idx, item in enumerate(self._queue):
            if item.schedule_id == schedule_id:
                self._queue.pop(idx)
                self._pending_schedule_ids.discard(schedule_id)
                return True
        return False

    def queued_items(self) -> list[QueuedRunView]:
        """Return queued items with their current 1-based positions."""
        return [
            QueuedRunView(
                kind=item.kind,
                queue_position=index,
                created_at=item.created_at,
                schedule_id=item.schedule_id,
                job_id=item.job_id,
                manual_trigger=item.manual_trigger,
            )
            for index, item in enumerate(self._queue, start=1)
        ]

    def is_schedule_active_or_queued(self, schedule_id: int) -> bool:
        """Return whether a schedule is currently running or waiting in queue."""
        return (
            schedule_id in self._active_schedule_ids
            or schedule_id in self._pending_schedule_ids
            or any(item.schedule_id == schedule_id for item in self._queue)
        )
