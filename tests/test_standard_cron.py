"""Standard CRON semantics are identical for validation, scheduling and catch-up."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

from scheduling.cron import StandardCronTrigger, previous_fire_time


@pytest.mark.parametrize(
    "expression",
    [
        "30 7 * * 1,4",
        "30 7 * * 0",
        "30 7 * * 7",
        "30 7 * * 0-6",
        "30 7 * * 1-5/2",
        "30 7 * * */2",
        "30 7 * * mon,thu",
        "30 7 * * sun-tue",
        "30 7 1 * mon",
        "0 0 29 feb *",
        "*/15 7-9 * jan,mar mon-fri",
    ],
)
def test_trigger_and_catchup_share_standard_cron_dates(expression):
    now = datetime(2026, 10, 23, 0, 0, 1, tzinfo=ZoneInfo("Asia/Taipei"))
    trigger = StandardCronTrigger(expression, "Asia/Taipei")
    reference = croniter(expression, now)
    previous = None
    for _ in range(30):
        fire = trigger.get_next_fire_time(previous, now)
        assert fire == reference.get_next(datetime)
        assert previous_fire_time(expression, fire + timedelta(seconds=1)) == fire
        previous = fire


def test_day_of_month_and_weekday_are_or_not_and():
    trigger = StandardCronTrigger("0 9 1 * mon", "Asia/Taipei")
    now = datetime(2026, 10, 23, tzinfo=ZoneInfo("Asia/Taipei"))
    first = trigger.get_next_fire_time(None, now)
    second = trigger.get_next_fire_time(first, now)
    assert first == datetime(2026, 10, 26, 9, tzinfo=now.tzinfo)  # Monday, not day 1
    assert second == datetime(2026, 11, 1, 9, tzinfo=now.tzinfo)  # Day 1, not Monday


def test_initial_exact_minute_is_included_and_subsequent_fire_advances():
    trigger = StandardCronTrigger("* * * * *")
    now = datetime(2026, 10, 23, tzinfo=ZoneInfo("UTC"))
    assert trigger.get_next_fire_time(None, now) == now
    assert trigger.get_next_fire_time(now, now) == now + timedelta(minutes=1)


@pytest.mark.parametrize(
    "expression",
    ["* * * *", "* * * * * *", "0 24 * * *", "0 1 * * 8", "*/0 * * * *", "0 0 31 2 *", "0 0 * * mon#2"],
)
def test_invalid_or_nonstandard_cron_is_rejected_before_registration(expression):
    with pytest.raises(ValueError):
        StandardCronTrigger(expression)
