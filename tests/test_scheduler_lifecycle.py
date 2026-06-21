"""Tests for schedule lifecycle timestamps and catch-up behavior."""

from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scheduling.scheduler as scheduler_module
from database.migrations import run_schema_migrations
from database.models import Base, JobStatus, Meeting, RecordingJob, Schedule
from scheduling.scheduler import SchedulerService
from utils.timezone import utc_now


@pytest.fixture
def session_local(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(scheduler_module, "get_session_local", lambda: SessionLocal)
    return SessionLocal


def create_schedule(session_local, **kwargs) -> int:
    session = session_local()
    try:
        meeting = Meeting(
            name="Lifecycle Meeting",
            provider="jitsi",
            meeting_code="lifecycle-room",
            default_display_name="Recorder Bot",
        )
        schedule = Schedule(
            meeting=meeting,
            schedule_type=kwargs.pop("schedule_type", "cron"),
            cron_expression=kwargs.pop("cron_expression", "* * * * *"),
            duration_sec=kwargs.pop("duration_sec", 3600),
            enabled=True,
            **kwargs,
        )
        session.add(meeting)
        session.add(schedule)
        session.commit()
        return schedule.id
    finally:
        session.close()


def test_scheduler_service_does_not_keep_unused_job_inspection_wrappers():
    assert not hasattr(SchedulerService, "get_next_run_time")
    assert not hasattr(SchedulerService, "get_all_jobs")


def test_schema_migration_adds_schedule_lifecycle_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE recording_jobs (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE detection_logs (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE schedules (id INTEGER PRIMARY KEY)")

    run_schema_migrations(engine)

    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(schedules)").fetchall()}
        job_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(recording_jobs)").fetchall()}

    assert {"last_triggered_at", "last_started_at", "last_completed_at"} <= columns
    assert {"local_recording_deleted_at", "local_recording_cleanup_reason"} <= job_columns


@pytest.mark.asyncio
async def test_schedule_trigger_updates_triggered_not_last_run(session_local):
    schedule_id = create_schedule(session_local, last_run_at=utc_now() - timedelta(hours=1))
    callback_calls = []
    scheduler = SchedulerService()
    scheduler.set_job_callback(lambda schedule_id: callback_calls.append(schedule_id))

    await scheduler._on_schedule_trigger(schedule_id)

    session = session_local()
    try:
        schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        assert schedule.last_triggered_at is not None
        assert schedule.last_run_at is not None
        assert schedule.last_run_at < schedule.last_triggered_at
    finally:
        session.close()
    assert callback_calls == [schedule_id]


def test_catchup_does_not_skip_just_because_schedule_was_triggered(session_local):
    schedule_id = create_schedule(
        session_local,
        last_triggered_at=utc_now(),
        last_run_at=utc_now(),
    )
    callback_calls = []
    scheduler = SchedulerService()
    scheduler._started = True
    scheduler._scheduler = SimpleNamespace(
        timezone=ZoneInfo("UTC"),
        add_job=lambda *args, **kwargs: None,
        get_job=lambda _job_id: None,
        remove_job=lambda _job_id: None,
    )
    scheduler.set_job_callback(lambda schedule_id: callback_calls.append(schedule_id))

    session = session_local()
    try:
        schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        scheduler.add_schedule(schedule)
    finally:
        session.close()

    assert callback_calls == [schedule_id]


def test_catchup_skips_existing_succeeded_job(session_local):
    schedule_id = create_schedule(session_local)
    session = session_local()
    try:
        job = RecordingJob(
            job_id="done123",
            schedule_id=schedule_id,
            provider="jitsi",
            meeting_code="lifecycle-room",
            display_name="Recorder Bot",
            duration_sec=120,
            status=JobStatus.SUCCEEDED.value,
            created_at=utc_now(),
        )
        session.add(job)
        session.commit()
    finally:
        session.close()

    callback_calls = []
    scheduler = SchedulerService()
    scheduler._started = True
    scheduler._scheduler = SimpleNamespace(
        timezone=ZoneInfo("UTC"),
        add_job=lambda *args, **kwargs: None,
        get_job=lambda _job_id: None,
        remove_job=lambda _job_id: None,
    )
    scheduler.set_job_callback(lambda schedule_id: callback_calls.append(schedule_id))

    session = session_local()
    try:
        schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
        scheduler.add_schedule(schedule)
    finally:
        session.close()

    assert callback_calls == []


def test_sync_all_next_run_times_batches_commit_and_skips_unchanged(session_local, monkeypatch):
    unchanged_next_run = utc_now() + timedelta(minutes=10)
    changed_next_run = utc_now() + timedelta(minutes=20)
    unchanged_id = create_schedule(session_local, next_run_at=unchanged_next_run)
    changed_id = create_schedule(session_local, next_run_at=None)

    commit_calls = []
    created_sessions = []

    class CountingSession:
        def __init__(self):
            self._session = session_local()
            created_sessions.append(self)

        def __getattr__(self, name):
            return getattr(self._session, name)

        def commit(self):
            commit_calls.append("commit")
            return self._session.commit()

        def close(self):
            return self._session.close()

    monkeypatch.setattr(scheduler_module, "get_session_local", lambda: CountingSession)

    scheduler = SchedulerService()
    scheduler._scheduler = SimpleNamespace(
        get_jobs=lambda: [
            SimpleNamespace(id=f"schedule_{unchanged_id}", next_run_time=unchanged_next_run),
            SimpleNamespace(id=f"schedule_{changed_id}", next_run_time=changed_next_run),
            SimpleNamespace(id="schedule_sync", next_run_time=changed_next_run),
        ]
    )

    scheduler._sync_all_next_run_times()

    session = session_local()
    try:
        unchanged = session.query(Schedule).filter(Schedule.id == unchanged_id).first()
        changed = session.query(Schedule).filter(Schedule.id == changed_id).first()
        assert unchanged.next_run_at == unchanged_next_run.replace(tzinfo=None)
        assert changed.next_run_at == changed_next_run.replace(tzinfo=None)
    finally:
        session.close()

    assert len(created_sessions) == 1
    assert commit_calls == ["commit"]


def test_sync_all_next_run_times_does_not_commit_when_values_are_unchanged(session_local, monkeypatch):
    next_run = utc_now() + timedelta(minutes=10)
    schedule_id = create_schedule(session_local, next_run_at=next_run)
    commit_calls = []

    class CountingSession:
        def __init__(self):
            self._session = session_local()

        def __getattr__(self, name):
            return getattr(self._session, name)

        def commit(self):
            commit_calls.append("commit")
            return self._session.commit()

        def close(self):
            return self._session.close()

    monkeypatch.setattr(scheduler_module, "get_session_local", lambda: CountingSession)

    scheduler = SchedulerService()
    scheduler._scheduler = SimpleNamespace(
        get_jobs=lambda: [SimpleNamespace(id=f"schedule_{schedule_id}", next_run_time=next_run)]
    )

    scheduler._sync_all_next_run_times()

    assert commit_calls == []
