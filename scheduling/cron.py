"""Standard five-field CRON semantics shared by validation and execution."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.base import BaseTrigger
from croniter import CroniterBadCronError, CroniterBadDateError, croniter

_STANDARD_FIELD = re.compile(r"(?:\*|[a-z]{3}|\d+)(?:-(?:[a-z]{3}|\d+))?(?:/\d+)?", re.IGNORECASE)


def _validate(expression: str) -> str:
    if not isinstance(expression, str):
        raise ValueError("CRON expression must be a string")
    fields = expression.split()
    if len(fields) != 5 or any(
        not all(_STANDARD_FIELD.fullmatch(item) for item in field.split(",")) for field in fields
    ):
        raise ValueError("CRON must contain five standard minute/hour/day/month/weekday fields")
    normalized = " ".join(fields).lower()
    if not croniter.is_valid(normalized):
        raise ValueError("Invalid CRON expression")
    return normalized


def previous_fire_time(expression: str, before: datetime) -> datetime:
    """Return the strictly previous fire using the same DOM/DOW OR rules as runtime."""
    return croniter(_validate(expression), before, day_or=True, max_years_between_matches=8).get_prev(datetime)


class StandardCronTrigger(BaseTrigger):
    """APScheduler adapter using standard CRON weekdays and DOM/DOW OR semantics.

    croniter owns next and previous fire calculation. Never translate weekday
    digits into CronTrigger syntax: ranges, steps and Sunday 7 would change.
    """

    def __init__(self, expression: str, timezone="UTC"):
        self.expression = _validate(expression)
        self.timezone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        # Compile before persistence, including syntactically valid impossible
        # dates (e.g. February 31). Eight years covers leap-day schedules.
        try:
            croniter(
                self.expression,
                datetime(2024, 1, 1, tzinfo=self.timezone),
                day_or=True,
                max_years_between_matches=8,
            ).get_next(datetime)
        except (CroniterBadCronError, CroniterBadDateError) as exc:
            raise ValueError("CRON has no valid firing date") from exc

    def get_next_fire_time(self, previous_fire_time, now):
        local_now = now.astimezone(self.timezone)
        # APScheduler includes now on the first lookup, but subsequent lookups
        # must advance beyond the previous fire (including coalesced misfires).
        start = (
            previous_fire_time.astimezone(self.timezone)
            if previous_fire_time
            else local_now - timedelta(microseconds=1)
        )
        return croniter(self.expression, start, day_or=True, max_years_between_matches=8).get_next(datetime)

    def __str__(self):
        return f"standard_cron[{self.expression}, timezone={self.timezone}]"
