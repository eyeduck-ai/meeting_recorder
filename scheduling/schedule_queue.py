"""In-memory queue state for scheduled recording runs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class QueueScheduleResult:
    """Result of attempting to enqueue a schedule recording."""

    accepted: bool
    status: str
    schedule_id: int
    queue_position: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class ScheduledQueueItem:
    """A queued schedule run request."""

    schedule_id: int
    manual_trigger: bool


class ScheduleRunQueue:
    """Track scheduled run queue, pending ids, and active schedule id."""

    def __init__(self) -> None:
        self._current_schedule_id: int | None = None
        self._queue: list[ScheduledQueueItem] = []
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

    def enqueue(
        self,
        schedule_id: int,
        *,
        manual_trigger: bool = False,
        lock_busy: bool = False,
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

        busy = (
            lock_busy or self._current_schedule_id is not None or bool(self._pending_schedule_ids) or bool(self._queue)
        )
        status = "queued" if busy else "triggered"
        if not busy:
            queue_position = 0
        elif self._current_schedule_id is None and not lock_busy and self._queue:
            queue_position = self.queue_length
        else:
            queue_position = self.queue_length + 1

        self._queue.append(ScheduledQueueItem(schedule_id=schedule_id, manual_trigger=manual_trigger))
        self._pending_schedule_ids.add(schedule_id)
        return QueueScheduleResult(
            accepted=True,
            status=status,
            schedule_id=schedule_id,
            queue_position=queue_position,
        )

    def pop_next(self) -> ScheduledQueueItem | None:
        """Move the next queued item into the current active slot."""
        if not self._queue:
            return None

        item = self._queue.pop(0)
        self._pending_schedule_ids.discard(item.schedule_id)
        self._current_schedule_id = item.schedule_id
        return item

    def mark_current_done(self) -> None:
        """Clear the current active schedule slot."""
        self._current_schedule_id = None

    def is_schedule_active_or_queued(self, schedule_id: int) -> bool:
        """Return whether a schedule is currently running or waiting in queue."""
        return (
            self._current_schedule_id == schedule_id
            or schedule_id in self._pending_schedule_ids
            or any(item.schedule_id == schedule_id for item in self._queue)
        )
