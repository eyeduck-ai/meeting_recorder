"""Tests for scheduled run queue state."""

from scheduling.schedule_queue import QueueScheduleResult, ScheduleRunQueue


def test_first_enqueue_is_triggered_then_pending_makes_next_queued():
    queue = ScheduleRunQueue()

    first = queue.enqueue(1, manual_trigger=True, lock_busy=False)
    second = queue.enqueue(2, manual_trigger=True, lock_busy=False)

    assert first == QueueScheduleResult(
        accepted=True,
        status="triggered",
        schedule_id=1,
        queue_position=0,
    )
    assert second == QueueScheduleResult(
        accepted=True,
        status="queued",
        schedule_id=2,
        queue_position=1,
    )
    assert queue.queue_length == 2


def test_duplicate_active_or_queued_schedule_is_rejected():
    queue = ScheduleRunQueue()

    queue.enqueue(1, manual_trigger=True)
    queued_duplicate = queue.enqueue(1, manual_trigger=True)

    active = queue.pop_next()
    active_duplicate = queue.enqueue(1, manual_trigger=True)

    assert active is not None
    assert active.schedule_id == 1
    assert queued_duplicate == QueueScheduleResult(
        accepted=False,
        status="duplicate",
        schedule_id=1,
        reason="Schedule is already running or queued",
    )
    assert active_duplicate == queued_duplicate


def test_pop_next_clears_pending_and_mark_current_done_clears_active():
    queue = ScheduleRunQueue()

    queue.enqueue(1, manual_trigger=False)
    item = queue.pop_next()

    assert item is not None
    assert item.schedule_id == 1
    assert queue.current_schedule_id == 1
    assert queue.is_schedule_active_or_queued(1) is True

    queue.mark_current_done()

    assert queue.current_schedule_id is None
    assert queue.is_schedule_active_or_queued(1) is False
