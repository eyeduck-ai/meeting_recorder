import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config.settings import get_settings
from database.models import Schedule, ScheduleType, get_session_local
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)


def convert_cron_weekday(cron_expression: str) -> str:
    """Convert standard CRON weekday (0=Sun) to APScheduler format (0=Mon).

    Standard CRON: 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat
    APScheduler:   0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

    This function converts the weekday field so that user can use familiar
    standard CRON format while APScheduler executes correctly.
    """
    parts = cron_expression.split()
    if len(parts) != 5:
        return cron_expression  # Invalid format, return as-is

    minute, hour, day, month, weekday = parts

    # Convert weekday field
    def convert_day(match):
        day_num = int(match.group())
        # 0 (Sun) -> 6, 1 (Mon) -> 0, 2 (Tue) -> 1, etc.
        if day_num == 0:
            return "6"  # Sunday
        return str(day_num - 1)

    # Handle ranges like 1-5, lists like 1,4, and single values
    converted_weekday = re.sub(r"\d+", convert_day, weekday)

    return f"{minute} {hour} {day} {month} {converted_weekday}"


class SchedulerService:
    """APScheduler service for managing scheduled recordings."""

    def __init__(self):
        self._scheduler: AsyncIOScheduler | None = None
        self._job_callback: Callable[[int], None] | None = None
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started and self._scheduler is not None

    def set_job_callback(self, callback: Callable[[int], None]) -> None:
        """Set callback to be called when a scheduled job triggers.

        Args:
            callback: Function that takes schedule_id as argument
        """
        self._job_callback = callback

    def start(self) -> None:
        """Start the scheduler."""
        if self._started:
            logger.warning("Scheduler already started")
            return

        # Get timezone from settings
        settings = get_settings()
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            logger.warning(f"Invalid timezone {settings.timezone}, using UTC")
            tz = ZoneInfo("UTC")

        jobstores = {"default": MemoryJobStore()}
        executors = {"default": AsyncIOExecutor()}
        job_defaults = {
            "coalesce": True,  # Combine multiple missed runs into one
            "max_instances": 1,  # Single concurrency per job
            "misfire_grace_time": 300,  # 5 minutes grace period
        }

        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone=tz,
        )

        self._scheduler.start()
        self._started = True
        logger.info("Scheduler started")

        # Load existing schedules from database
        self._load_schedules_from_db()

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self._started = False
        logger.info("Scheduler stopped")

    def _load_schedules_from_db(self) -> None:
        """Load all enabled schedules from database."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            schedules = session.query(Schedule).filter(Schedule.enabled == True).all()
            for schedule in schedules:
                self.add_schedule(schedule)
            logger.info(f"Loaded {len(schedules)} schedules from database")
        finally:
            session.close()

    def add_schedule(self, schedule: Schedule) -> str | None:
        """Add a schedule to the scheduler.

        Args:
            schedule: Schedule model instance

        Returns:
            APScheduler job ID or None if failed
        """
        if not self._scheduler:
            logger.error("Scheduler not started")
            return None

        job_id = f"schedule_{schedule.id}"

        # Remove existing job if any
        self.remove_schedule(schedule.id)

        try:
            # Normalize schedule_type to string value for comparison
            schedule_type_value = (
                schedule.schedule_type.value if hasattr(schedule.schedule_type, "value") else schedule.schedule_type
            )

            if schedule_type_value == ScheduleType.ONCE.value:
                # One-time schedule - trigger early_join_sec before start_time
                early_join = timedelta(seconds=schedule.early_join_sec)
                # Ensure trigger_time is timezone-aware UTC (DB stores naive UTC)
                trigger_time = ensure_utc(schedule.start_time) - early_join

                if trigger_time <= utc_now():
                    logger.warning(f"Schedule {schedule.id} trigger_time is in the past, skipping")
                    return None

                trigger = DateTrigger(run_date=trigger_time)
                logger.info(f"Schedule {schedule.id} will trigger {schedule.early_join_sec}s early at {trigger_time}")

            elif schedule_type_value == ScheduleType.CRON.value:
                # Cron schedule
                if not schedule.cron_expression:
                    logger.error(f"Schedule {schedule.id} has no cron expression")
                    return None

                # Convert standard CRON weekday (0=Sun) to APScheduler format (0=Mon)
                converted_cron = convert_cron_weekday(schedule.cron_expression)
                trigger = CronTrigger.from_crontab(converted_cron)

            else:
                logger.error(f"Unknown schedule type: {schedule.schedule_type}")
                return None

            # Use duration_sec as misfire grace time so that if system starts late
            # (e.g., after reboot), it will still run the job within the recording window
            grace_time = schedule.duration_sec

            self._scheduler.add_job(
                self._on_schedule_trigger,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                name=f"Recording schedule {schedule.id}",
                replace_existing=True,
                misfire_grace_time=grace_time,
            )

            # Update next_run_at
            job = self._scheduler.get_job(job_id)
            if job and job.next_run_time:
                self._update_next_run(schedule.id, job.next_run_time)

            logger.info(f"Added schedule {schedule.id} ({schedule.schedule_type})")
            return job_id

        except Exception as e:
            logger.error(f"Failed to add schedule {schedule.id}: {e}")
            return None

    def remove_schedule(self, schedule_id: int) -> bool:
        """Remove a schedule from the scheduler.

        Args:
            schedule_id: Schedule ID

        Returns:
            True if removed, False otherwise
        """
        if not self._scheduler:
            return False

        job_id = f"schedule_{schedule_id}"
        try:
            self._scheduler.remove_job(job_id)
            logger.info(f"Removed schedule {schedule_id}")
            return True
        except Exception:
            return False

    def update_schedule(self, schedule: Schedule) -> bool:
        """Update a schedule in the scheduler.

        Args:
            schedule: Updated Schedule model instance

        Returns:
            True if updated successfully
        """
        if schedule.enabled:
            return self.add_schedule(schedule) is not None
        else:
            return self.remove_schedule(schedule.id)

    async def _on_schedule_trigger(self, schedule_id: int) -> None:
        """Called when a scheduled job triggers.

        Args:
            schedule_id: ID of the triggered schedule
        """
        logger.info(f"Schedule {schedule_id} triggered")

        # Update last_run_at
        self._update_last_run(schedule_id)

        # Call the registered callback
        if self._job_callback:
            try:
                self._job_callback(schedule_id)
            except Exception as e:
                logger.error(f"Job callback error for schedule {schedule_id}: {e}")

        # Update next_run_at for cron jobs
        if self._scheduler:
            job_id = f"schedule_{schedule_id}"
            job = self._scheduler.get_job(job_id)
            if job and job.next_run_time:
                self._update_next_run(schedule_id, job.next_run_time)

    def _update_last_run(self, schedule_id: int) -> None:
        """Update last_run_at in database."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
            if schedule:
                schedule.last_run_at = utc_now()
                session.commit()
        finally:
            session.close()

    def _update_next_run(self, schedule_id: int, next_run: datetime) -> None:
        """Update next_run_at in database."""
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
            if schedule:
                # Ensure next_run is UTC-aware (scheduler provides local time)
                schedule.next_run_at = ensure_utc(next_run)
                session.commit()
        finally:
            session.close()

    def get_next_run_time(self, schedule_id: int) -> datetime | None:
        """Get next run time for a schedule.

        Args:
            schedule_id: Schedule ID

        Returns:
            Next run time or None
        """
        if not self._scheduler:
            return None

        job_id = f"schedule_{schedule_id}"
        job = self._scheduler.get_job(job_id)
        return job.next_run_time if job else None

    def get_all_jobs(self) -> list[dict]:
        """Get all scheduled jobs.

        Returns:
            List of job info dictionaries
        """
        if not self._scheduler:
            return []

        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                    "trigger": str(job.trigger),
                }
            )
        return jobs

    async def trigger_schedule(self, schedule_id: int) -> str | None:
        """Manually trigger a schedule immediately.

        Args:
            schedule_id: Schedule ID to trigger

        Returns:
            Job ID if triggered successfully, None otherwise
        """
        logger.info(f"Manual trigger for schedule {schedule_id}")

        # Update last_run_at
        self._update_last_run(schedule_id)

        # Call the registered callback
        if self._job_callback:
            try:
                result = self._job_callback(schedule_id)
                return result
            except Exception as e:
                logger.error(f"Job callback error for schedule {schedule_id}: {e}")
                return None

        return None


# Global scheduler instance
_scheduler_instance: SchedulerService | None = None


def get_scheduler() -> SchedulerService:
    """Get the global scheduler instance."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = SchedulerService()
    return _scheduler_instance
