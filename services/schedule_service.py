"""Service layer for schedule write and trigger operations."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from database.models import Meeting, Schedule, ScheduleType
from services.errors import NotFoundError, ValidationError
from services.runtime_config import RuntimeConfigError, RuntimeConfigService, get_runtime_config_service
from utils.timezone import ensure_utc, utc_now

MIN_SCHEDULE_DURATION_SEC = 60
MAX_SCHEDULE_DURATION_SEC = 14400


@dataclass(frozen=True)
class ScheduleCreateData:
    """Fields used to create a schedule."""

    meeting_id: int
    schedule_type: str = ScheduleType.ONCE.value
    start_time: datetime | None = None
    duration_sec: int = 4200
    cron_expression: str | None = None
    lobby_wait_sec: int | None = None
    layout_preset: str = "speaker"
    resolution_w: int | None = None
    resolution_h: int | None = None
    override_meeting_code: str | None = None
    override_display_name: str | None = None
    override_guest_name: str | None = None
    override_guest_email: str | None = None
    youtube_enabled: bool = False
    youtube_privacy: str = "unlisted"
    early_join_sec: int = 30
    smart_trim_enabled: bool | None = None
    dynamic_extension_enabled: bool | None = None
    dynamic_extension_idle_sec: int | None = None
    dynamic_extension_max_sec: int | None = None
    enabled: bool = True


class ScheduleService:
    """Coordinate schedule persistence, runtime config, and scheduler sync."""

    def __init__(
        self,
        *,
        scheduler=None,
        job_runner=None,
        runtime_config_service: RuntimeConfigService | None = None,
    ):
        self._scheduler = scheduler
        self._job_runner = job_runner
        self._runtime_config_service = runtime_config_service or get_runtime_config_service()

    def create_schedule(self, db: Session, data: ScheduleCreateData, *, sync_scheduler: bool = True) -> Schedule:
        """Create a schedule and optionally sync it to APScheduler."""
        self._require_meeting(db, data.meeting_id)
        self._validate_cron(data.schedule_type, data.cron_expression)
        self._validate_duration_sec(data.duration_sec)

        runtime_config = self._resolve_runtime_config_for_schedule(
            db,
            lobby_wait_sec=data.lobby_wait_sec,
            resolution_w=data.resolution_w,
            resolution_h=data.resolution_h,
            smart_trim_enabled=data.smart_trim_enabled,
            dynamic_extension_enabled=data.dynamic_extension_enabled,
            dynamic_extension_idle_sec=data.dynamic_extension_idle_sec,
            dynamic_extension_max_sec=data.dynamic_extension_max_sec,
        )

        schedule = Schedule(
            meeting_id=data.meeting_id,
            schedule_type=self._schedule_type_value(data.schedule_type),
            start_time=data.start_time,
            duration_sec=data.duration_sec,
            duration_mode="fixed",
            cron_expression=data.cron_expression,
            lobby_wait_sec=runtime_config.lobby_wait_sec,
            layout_preset=data.layout_preset,
            resolution_w=runtime_config.resolution_w,
            resolution_h=runtime_config.resolution_h,
            override_meeting_code=data.override_meeting_code,
            override_display_name=data.override_display_name,
            override_guest_name=data.override_guest_name,
            override_guest_email=data.override_guest_email,
            youtube_enabled=data.youtube_enabled,
            youtube_privacy=data.youtube_privacy,
            early_join_sec=data.early_join_sec,
            min_duration_sec=None,
            stillness_timeout_sec=180,
            auto_detect_mode=None,
            dry_run=False,
            smart_trim_enabled=data.smart_trim_enabled,
            dynamic_extension_enabled=data.dynamic_extension_enabled,
            dynamic_extension_idle_sec=data.dynamic_extension_idle_sec,
            dynamic_extension_max_sec=data.dynamic_extension_max_sec,
            enabled=data.enabled,
        )
        db.add(schedule)
        db.commit()
        db.refresh(schedule)

        if sync_scheduler:
            self._sync_after_create(schedule)

        return schedule

    def update_schedule(self, db: Session, schedule_id: int, updates: dict[str, Any]) -> Schedule:
        """Update a schedule and sync APScheduler state."""
        schedule = self._get_schedule(db, schedule_id)
        update_data = dict(updates)
        update_data["duration_mode"] = "fixed"
        update_data["auto_detect_mode"] = None
        update_data["dry_run"] = False

        if "meeting_id" in update_data:
            self._require_meeting(db, update_data["meeting_id"])

        if "schedule_type" in update_data:
            update_data["schedule_type"] = self._schedule_type_value(update_data["schedule_type"])

        if "duration_sec" in update_data:
            self._validate_duration_sec(update_data["duration_sec"])

        resulting_type = update_data.get("schedule_type", schedule.schedule_type)
        resulting_cron = update_data.get("cron_expression", schedule.cron_expression)
        self._validate_cron(resulting_type, resulting_cron)

        self._apply_runtime_updates(db, schedule, update_data)

        for field, value in update_data.items():
            setattr(schedule, field, value)

        db.commit()
        db.refresh(schedule)
        self._sync_after_save(schedule)
        return schedule

    def delete_schedule(self, db: Session, schedule_id: int) -> None:
        """Delete a schedule and remove it from APScheduler."""
        schedule = self._get_schedule(db, schedule_id)
        scheduler = self._get_scheduler()
        if scheduler.is_running:
            scheduler.remove_schedule(schedule_id)
        db.delete(schedule)
        db.commit()

    def delete_expired_once_schedules(self, db: Session, now: datetime) -> int:
        """Delete expired one-time schedules and return the number removed."""
        scheduler = self._get_scheduler()
        deleted = 0
        schedules = db.query(Schedule).all()
        for schedule in schedules:
            if not self._is_expired_once(schedule, now):
                continue
            if scheduler.is_running:
                scheduler.remove_schedule(schedule.id)
            db.delete(schedule)
            deleted += 1
        db.commit()
        return deleted

    def set_enabled(self, db: Session, schedule_id: int, enabled: bool) -> Schedule:
        """Enable or disable a schedule and sync APScheduler."""
        schedule = self._get_schedule(db, schedule_id)
        schedule.enabled = enabled
        db.commit()
        db.refresh(schedule)

        scheduler = self._get_scheduler()
        if scheduler.is_running:
            if enabled:
                scheduler.add_schedule(schedule)
            else:
                scheduler.remove_schedule(schedule_id)
        return schedule

    def toggle_enabled(self, db: Session, schedule_id: int) -> Schedule:
        """Toggle a schedule's enabled state."""
        schedule = self._get_schedule(db, schedule_id)
        return self.set_enabled(db, schedule_id, not schedule.enabled)

    def trigger_schedule(self, db: Session, schedule_id: int, *, manual_trigger: bool = True):
        """Record a manual trigger and enqueue the schedule."""
        schedule = self._get_schedule(db, schedule_id)
        schedule.last_triggered_at = utc_now()
        db.commit()

        return self._get_job_runner().queue_schedule(schedule_id, manual_trigger=manual_trigger)

    def _apply_runtime_updates(self, db: Session, schedule: Schedule, update_data: dict[str, Any]) -> None:
        runtime_keys = {
            "lobby_wait_sec",
            "resolution_w",
            "resolution_h",
            "smart_trim_enabled",
            "dynamic_extension_enabled",
            "dynamic_extension_idle_sec",
            "dynamic_extension_max_sec",
        }
        if not runtime_keys.intersection(update_data):
            return

        runtime_config = self._resolve_runtime_config_for_schedule(
            db,
            lobby_wait_sec=update_data.get("lobby_wait_sec", schedule.lobby_wait_sec),
            resolution_w=update_data.get("resolution_w", schedule.resolution_w),
            resolution_h=update_data.get("resolution_h", schedule.resolution_h),
            smart_trim_enabled=update_data.get("smart_trim_enabled", schedule.smart_trim_enabled),
            dynamic_extension_enabled=update_data.get("dynamic_extension_enabled", schedule.dynamic_extension_enabled),
            dynamic_extension_idle_sec=update_data.get(
                "dynamic_extension_idle_sec", schedule.dynamic_extension_idle_sec
            ),
            dynamic_extension_max_sec=update_data.get("dynamic_extension_max_sec", schedule.dynamic_extension_max_sec),
        )
        update_data["lobby_wait_sec"] = runtime_config.lobby_wait_sec
        update_data["resolution_w"] = runtime_config.resolution_w
        update_data["resolution_h"] = runtime_config.resolution_h

    def _resolve_runtime_config_for_schedule(
        self,
        db: Session,
        *,
        lobby_wait_sec: int | None,
        resolution_w: int | None,
        resolution_h: int | None,
        smart_trim_enabled: bool | None,
        dynamic_extension_enabled: bool | None,
        dynamic_extension_idle_sec: int | None,
        dynamic_extension_max_sec: int | None,
    ):
        try:
            return self._runtime_config_service.get_recording_config(
                db,
                lobby_wait_sec=lobby_wait_sec,
                resolution_w=resolution_w,
                resolution_h=resolution_h,
                smart_trim_enabled=smart_trim_enabled,
                dynamic_extension_enabled=dynamic_extension_enabled,
                dynamic_extension_idle_sec=dynamic_extension_idle_sec,
                dynamic_extension_max_sec=dynamic_extension_max_sec,
            )
        except RuntimeConfigError as exc:
            raise ValidationError(str(exc)) from exc

    def _sync_after_save(self, schedule: Schedule) -> None:
        scheduler = self._get_scheduler()
        if not scheduler.is_running:
            return
        if schedule.enabled:
            scheduler.update_schedule(schedule)
        else:
            scheduler.remove_schedule(schedule.id)

    def _sync_after_create(self, schedule: Schedule) -> None:
        scheduler = self._get_scheduler()
        if scheduler.is_running and schedule.enabled:
            scheduler.add_schedule(schedule)

    def _require_meeting(self, db: Session, meeting_id: int) -> Meeting:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            raise NotFoundError("Meeting not found")
        return meeting

    def _get_schedule(self, db: Session, schedule_id: int) -> Schedule:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            raise NotFoundError("Schedule not found")
        return schedule

    def _get_scheduler(self):
        if self._scheduler is None:
            from scheduling.scheduler import get_scheduler

            return get_scheduler()
        return self._scheduler

    def _get_job_runner(self):
        if self._job_runner is None:
            from scheduling.job_runner import get_job_runner

            return get_job_runner()
        return self._job_runner

    def _validate_cron(self, schedule_type: str, cron_expression: str | None) -> None:
        if self._schedule_type_value(schedule_type) == ScheduleType.CRON.value and not cron_expression:
            raise ValidationError("cron_expression is required for cron schedule type")

    def _validate_duration_sec(self, duration_sec: int) -> None:
        if isinstance(duration_sec, bool) or not isinstance(duration_sec, int):
            raise ValidationError("duration_sec must be an integer")
        if duration_sec < MIN_SCHEDULE_DURATION_SEC or duration_sec > MAX_SCHEDULE_DURATION_SEC:
            raise ValidationError(
                f"duration_sec must be between {MIN_SCHEDULE_DURATION_SEC} and {MAX_SCHEDULE_DURATION_SEC}"
            )

    def _schedule_type_value(self, schedule_type: str) -> str:
        return schedule_type.value if hasattr(schedule_type, "value") else str(schedule_type)

    def _is_expired_once(self, schedule: Schedule, now: datetime) -> bool:
        if self._schedule_type_value(schedule.schedule_type) != ScheduleType.ONCE.value:
            return False
        if schedule.start_time:
            start_time = ensure_utc(schedule.start_time)
            return bool(start_time and now >= start_time + timedelta(seconds=schedule.duration_sec))
        return not schedule.next_run_at


def get_schedule_service(**kwargs) -> ScheduleService:
    """Create a schedule service instance."""
    return ScheduleService(**kwargs)
