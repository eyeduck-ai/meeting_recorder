import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config.settings import get_settings
from database.models import Schedule, ScheduleType, get_session_local

logger = logging.getLogger(__name__)


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
            if schedule.schedule_type == ScheduleType.ONCE.value:
                # One-time schedule
                if schedule.start_time <= datetime.utcnow():
                    logger.warning(f"Schedule {schedule.id} start_time is in the past, skipping")
                    return None

                trigger = DateTrigger(run_date=schedule.start_time)

            elif schedule.schedule_type == ScheduleType.CRON.value:
                # Cron schedule
                if not schedule.cron_expression:
                    logger.error(f"Schedule {schedule.id} has no cron expression")
                    return None

                trigger = CronTrigger.from_crontab(schedule.cron_expression)

            else:
                logger.error(f"Unknown schedule type: {schedule.schedule_type}")
                return None

            self._scheduler.add_job(
                self._on_schedule_trigger,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                name=f"Recording schedule {schedule.id}",
                replace_existing=True,
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
                schedule.last_run_at = datetime.utcnow()
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
                schedule.next_run_at = next_run
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
