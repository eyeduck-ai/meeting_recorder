from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AppSettings, Base, JobStatus, Meeting, RecordingJob, Schedule
from scheduling.job_runner import QueueScheduleResult
from services.errors import ConflictError
from services.job_service import ImmediateRecordingData, JobService
from services.meeting_service import MeetingCreateData, MeetingService
from services.schedule_service import ScheduleCreateData, ScheduleService
from utils.timezone import utc_now


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'services.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def meeting(db_session):
    meeting = Meeting(
        name="Service Meeting",
        provider="jitsi",
        meeting_code="service-room",
        default_display_name="Recorder Bot",
    )
    db_session.add(meeting)
    db_session.commit()
    return meeting


class FakeScheduler:
    is_running = True

    def __init__(self):
        self.added = []
        self.updated = []
        self.removed = []

    def add_schedule(self, schedule):
        self.added.append(schedule.id)

    def update_schedule(self, schedule):
        self.updated.append(schedule.id)

    def remove_schedule(self, schedule_id):
        self.removed.append(schedule_id)


def test_meeting_service_maps_password_field(db_session):
    service = MeetingService()

    meeting = service.create_meeting(
        db_session,
        MeetingCreateData(
            name="Secret Meeting",
            provider="zoom",
            meeting_code="https://zoom.us/j/123",
            password="pw-1",
        ),
    )
    assert meeting.meeting_password_plaintext == "pw-1"
    assert meeting.has_password is True

    updated = service.update_meeting(db_session, meeting.id, {"password": "pw-2"})
    assert updated.meeting_password_plaintext == "pw-2"
    assert updated.has_password is True

    retained = service.update_meeting(db_session, meeting.id, {"name": "Renamed Secret Meeting"})
    assert retained.meeting_password_plaintext == "pw-2"

    cleared = service.update_meeting(db_session, meeting.id, {"password": None})
    assert cleared.meeting_password_plaintext is None
    assert cleared.has_password is False


def test_schedule_service_create_uses_runtime_defaults_and_adds_scheduler(db_session, meeting):
    scheduler = FakeScheduler()
    db_session.add_all(
        [
            AppSettings(key="resolution_w", value="1600"),
            AppSettings(key="resolution_h", value="900"),
            AppSettings(key="lobby_wait_sec", value="222"),
        ]
    )
    db_session.commit()

    schedule = ScheduleService(scheduler=scheduler).create_schedule(
        db_session,
        ScheduleCreateData(
            meeting_id=meeting.id,
            start_time=utc_now() + timedelta(hours=1),
        ),
    )

    assert schedule.resolution_w == 1600
    assert schedule.resolution_h == 900
    assert schedule.lobby_wait_sec == 222
    assert scheduler.added == [schedule.id]


def test_schedule_service_update_delete_and_toggle_sync_scheduler(db_session, meeting):
    scheduler = FakeScheduler()
    service = ScheduleService(scheduler=scheduler)
    schedule = service.create_schedule(
        db_session,
        ScheduleCreateData(
            meeting_id=meeting.id,
            start_time=utc_now() + timedelta(hours=1),
        ),
    )

    updated = service.update_schedule(db_session, schedule.id, {"resolution_w": 1280, "resolution_h": 720})
    assert updated.resolution_w == 1280
    assert updated.resolution_h == 720
    assert scheduler.updated == [schedule.id]

    disabled = service.toggle_enabled(db_session, schedule.id)
    assert disabled.enabled is False
    assert scheduler.removed == [schedule.id]

    enabled = service.toggle_enabled(db_session, schedule.id)
    assert enabled.enabled is True
    assert scheduler.added == [schedule.id, schedule.id]

    service.delete_schedule(db_session, schedule.id)
    assert scheduler.removed == [schedule.id, schedule.id]
    assert db_session.query(Schedule).filter(Schedule.id == schedule.id).first() is None


def test_schedule_service_trigger_marks_triggered_and_returns_queue_result(db_session, meeting):
    class FakeRunner:
        def queue_schedule(self, schedule_id, manual_trigger=False):
            return QueueScheduleResult(
                accepted=True,
                status="queued",
                schedule_id=schedule_id,
                queue_position=1,
            )

    schedule = ScheduleService(scheduler=FakeScheduler()).create_schedule(
        db_session,
        ScheduleCreateData(
            meeting_id=meeting.id,
            start_time=utc_now() + timedelta(hours=1),
        ),
    )

    result = ScheduleService(job_runner=FakeRunner()).trigger_schedule(db_session, schedule.id)

    assert result.status == "queued"
    db_session.refresh(schedule)
    assert schedule.last_triggered_at is not None


@pytest.mark.asyncio
async def test_job_service_start_immediate_returns_persisted_job(db_session):
    class FakeRunner:
        is_busy = False

        async def run_immediate(self, **_kwargs):
            db_session.add(
                RecordingJob(
                    job_id="job-service-1",
                    provider="jitsi",
                    meeting_code="room",
                    display_name="Recorder Bot",
                    duration_sec=3600,
                    status=JobStatus.QUEUED.value,
                )
            )
            db_session.commit()
            return "job-service-1"

    job = await JobService(job_runner=FakeRunner()).start_immediate_recording(
        db_session,
        ImmediateRecordingData(
            provider="jitsi",
            meeting_code="room",
            display_name="Recorder Bot",
            duration_sec=3600,
        ),
    )

    assert job.job_id == "job-service-1"


@pytest.mark.asyncio
async def test_job_service_busy_raises_conflict(db_session):
    class FakeRunner:
        is_busy = True

    with pytest.raises(ConflictError):
        await JobService(job_runner=FakeRunner()).start_immediate_recording(
            db_session,
            ImmediateRecordingData(
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder Bot",
                duration_sec=3600,
            ),
        )
