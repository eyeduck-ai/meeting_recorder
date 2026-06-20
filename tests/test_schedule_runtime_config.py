"""Tests for schedule API runtime defaults."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.routes.schedules as schedules_module
from api.routes.schedules import ScheduleCreate, ScheduleUpdate, create_schedule, trigger_schedule, update_schedule
from database.models import AppSettings, Base, Meeting, Schedule
from scheduling.job_runner import QueueScheduleResult
from utils.timezone import utc_now


class FakeScheduler:
    is_running = False


def _request(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schedule-runtime.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def meeting_id(db_session):
    meeting = Meeting(
        name="Runtime Schedule Meeting",
        provider="jitsi",
        meeting_code="runtime-room",
        default_display_name="Recorder Bot",
    )
    db_session.add(meeting)
    db_session.commit()
    return meeting.id


@pytest.mark.asyncio
async def test_create_schedule_uses_db_runtime_defaults(db_session, meeting_id):
    db_session.add_all(
        [
            AppSettings(key="resolution_w", value="1440"),
            AppSettings(key="resolution_h", value="900"),
            AppSettings(key="lobby_wait_sec", value="321"),
        ]
    )
    db_session.commit()

    response = await create_schedule(
        ScheduleCreate(
            meeting_id=meeting_id,
            start_time=utc_now() + timedelta(hours=1),
        ),
        http_request=_request(scheduler=FakeScheduler()),
        db=db_session,
    )

    assert response.resolution_w == 1440
    assert response.resolution_h == 900
    assert response.lobby_wait_sec == 321
    assert response.last_run_at is None
    assert response.last_triggered_at is None
    assert response.last_started_at is None
    assert response.last_completed_at is None


@pytest.mark.asyncio
async def test_create_schedule_accepts_smart_boundary_overrides(db_session, meeting_id):
    response = await create_schedule(
        ScheduleCreate(
            meeting_id=meeting_id,
            start_time=utc_now() + timedelta(hours=1),
            smart_trim_enabled=False,
            dynamic_extension_enabled=True,
            dynamic_extension_idle_sec=300,
            dynamic_extension_max_sec=3600,
        ),
        http_request=_request(scheduler=FakeScheduler()),
        db=db_session,
    )

    assert response.smart_trim_enabled is False
    assert response.dynamic_extension_enabled is True
    assert response.dynamic_extension_idle_sec == 300
    assert response.dynamic_extension_max_sec == 3600


@pytest.mark.asyncio
async def test_create_schedule_rejects_invalid_dynamic_extension_override(db_session, meeting_id):
    with pytest.raises(schedules_module.HTTPException) as exc:
        await create_schedule(
            ScheduleCreate(
                meeting_id=meeting_id,
                start_time=utc_now() + timedelta(hours=1),
                dynamic_extension_idle_sec=300,
                dynamic_extension_max_sec=120,
            ),
            http_request=_request(scheduler=FakeScheduler()),
            db=db_session,
        )

    assert exc.value.status_code == 400
    assert "dynamic_extension_max_sec" in exc.value.detail


@pytest.mark.asyncio
async def test_update_schedule_accepts_resolution_fields(db_session, meeting_id):
    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
        resolution_w=1920,
        resolution_h=1080,
        lobby_wait_sec=900,
    )
    db_session.add(schedule)
    db_session.commit()

    response = await update_schedule(
        schedule.id,
        ScheduleUpdate(resolution_w=1280, resolution_h=720),
        http_request=_request(scheduler=FakeScheduler()),
        db=db_session,
    )

    assert response.resolution_w == 1280
    assert response.resolution_h == 720


@pytest.mark.asyncio
async def test_update_schedule_accepts_smart_boundary_overrides(db_session, meeting_id):
    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
        resolution_w=1920,
        resolution_h=1080,
        lobby_wait_sec=900,
    )
    db_session.add(schedule)
    db_session.commit()

    response = await update_schedule(
        schedule.id,
        ScheduleUpdate(
            smart_trim_enabled=True,
            dynamic_extension_enabled=False,
            dynamic_extension_idle_sec=600,
            dynamic_extension_max_sec=1800,
        ),
        http_request=_request(scheduler=FakeScheduler()),
        db=db_session,
    )

    assert response.smart_trim_enabled is True
    assert response.dynamic_extension_enabled is False
    assert response.dynamic_extension_idle_sec == 600
    assert response.dynamic_extension_max_sec == 1800


@pytest.mark.asyncio
async def test_update_schedule_rejects_invalid_dynamic_extension_override(db_session, meeting_id):
    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
        resolution_w=1920,
        resolution_h=1080,
        lobby_wait_sec=900,
    )
    db_session.add(schedule)
    db_session.commit()

    with pytest.raises(schedules_module.HTTPException) as exc:
        await update_schedule(
            schedule.id,
            ScheduleUpdate(dynamic_extension_idle_sec=300, dynamic_extension_max_sec=120),
            http_request=_request(scheduler=FakeScheduler()),
            db=db_session,
        )

    assert exc.value.status_code == 400
    assert "dynamic_extension_max_sec" in exc.value.detail


def test_update_schedule_schema_rejects_invalid_duration():
    with pytest.raises(PydanticValidationError):
        ScheduleUpdate(duration_sec=59)

    with pytest.raises(PydanticValidationError):
        ScheduleUpdate(duration_sec=14401)


@pytest.mark.asyncio
async def test_update_schedule_rejects_explicit_null_duration(db_session, meeting_id):
    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
    )
    db_session.add(schedule)
    db_session.commit()

    with pytest.raises(schedules_module.HTTPException) as exc:
        await update_schedule(
            schedule.id,
            ScheduleUpdate(duration_sec=None),
            http_request=_request(scheduler=FakeScheduler()),
            db=db_session,
        )

    assert exc.value.status_code == 400
    assert "duration_sec" in exc.value.detail


@pytest.mark.asyncio
async def test_trigger_schedule_api_returns_triggered(db_session, meeting_id):
    class FakeRunner:
        def queue_schedule(self, schedule_id, manual_trigger=False):
            return QueueScheduleResult(
                accepted=True,
                status="triggered",
                schedule_id=schedule_id,
                queue_position=0,
            )

    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
    )
    db_session.add(schedule)
    db_session.commit()

    response = await trigger_schedule(
        schedule.id, http_request=_request(scheduler=FakeScheduler(), job_runner=FakeRunner()), db=db_session
    )

    assert response.status_code == 200
    assert b'"status":"triggered"' in response.body
    assert b'"queue_position":0' in response.body


@pytest.mark.asyncio
async def test_trigger_schedule_api_returns_queued(db_session, meeting_id):
    class FakeRunner:
        def queue_schedule(self, schedule_id, manual_trigger=False):
            return QueueScheduleResult(
                accepted=True,
                status="queued",
                schedule_id=schedule_id,
                queue_position=2,
            )

    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
    )
    db_session.add(schedule)
    db_session.commit()

    response = await trigger_schedule(
        schedule.id, http_request=_request(scheduler=FakeScheduler(), job_runner=FakeRunner()), db=db_session
    )

    assert response.status_code == 202
    assert b'"status":"queued"' in response.body
    assert b'"queue_position":2' in response.body


@pytest.mark.asyncio
async def test_trigger_schedule_api_rejects_duplicate(db_session, meeting_id):
    class FakeRunner:
        def queue_schedule(self, schedule_id, manual_trigger=False):
            return QueueScheduleResult(
                accepted=False,
                status="duplicate",
                schedule_id=schedule_id,
                reason="Schedule is already running or queued",
            )

    schedule = Schedule(
        meeting_id=meeting_id,
        schedule_type="once",
        start_time=utc_now() + timedelta(hours=1),
        duration_sec=3600,
    )
    db_session.add(schedule)
    db_session.commit()

    with pytest.raises(schedules_module.HTTPException) as exc:
        await trigger_schedule(
            schedule.id,
            http_request=_request(scheduler=FakeScheduler(), job_runner=FakeRunner()),
            db=db_session,
        )

    assert exc.value.status_code == 409
