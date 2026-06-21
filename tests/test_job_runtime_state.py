from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.migrations import run_schema_migrations
from database.models import Base, JobStatus
from database.models import RecordingJob as RecordingJobModel
from services.job_runtime_state import JobRuntimeStateService
from utils.timezone import utc_now


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "runtime-state.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _job(job_id: str, status: str, meeting_code: str) -> RecordingJobModel:
    return RecordingJobModel(
        job_id=job_id,
        provider="jitsi",
        meeting_code=meeting_code,
        display_name="Recorder Bot",
        duration_sec=3600,
        lobby_wait_sec=900,
        status=status,
        started_at=utc_now(),
    )


def _queued_item(kind: str, position: int, *, job_id: str | None = None, schedule_id: int | None = None):
    return SimpleNamespace(
        kind=kind,
        queue_position=position,
        job_id=job_id,
        schedule_id=schedule_id,
        manual_trigger=False,
        created_at=utc_now(),
    )


def _retry_item(job_id: str, *, schedule_id: int | None = None, retry_after_sec: int = 30):
    return SimpleNamespace(
        job_id=job_id,
        schedule_id=schedule_id,
        status="retry_waiting",
        retry_after_sec=retry_after_sec,
        meeting_code=None,
        display_name=None,
    )


def test_runtime_snapshot_builds_shared_state_and_excludes_stale_active_jobs(db_session):
    db_session.add_all(
        [
            _job("job-recording", JobStatus.RECORDING.value, "room-recording"),
            _job("job-uploading", JobStatus.UPLOADING.value, "room-uploading"),
            _job("job-succeeded", JobStatus.SUCCEEDED.value, "room-succeeded"),
            _job("job-queued", JobStatus.QUEUED.value, "room-queued"),
            _job("job-retry", JobStatus.QUEUED.value, "room-retry"),
        ]
    )
    db_session.commit()
    worker = SimpleNamespace(
        active_jobs=[
            SimpleNamespace(job_id="job-recording"),
            SimpleNamespace(job_id="job-uploading"),
            SimpleNamespace(job_id="job-succeeded"),
        ],
    )
    runner = SimpleNamespace(
        queued_items=[_queued_item("immediate", 1, job_id="job-queued")],
        retry_waiting_items=[_retry_item("job-retry", schedule_id=7, retry_after_sec=45)],
        retry_waiting_count=1,
        queue_length=1,
        max_concurrent_recordings=2,
        available_slots=0,
    )

    snapshot = JobRuntimeStateService().build_snapshot(db_session, worker=worker, runner=runner)

    assert [job.job_id for job in snapshot.active_jobs] == ["job-recording"]
    assert snapshot.active_job_ids == {"job-recording"}
    assert snapshot.queued_job_ids == {"job-queued"}
    assert snapshot.queued_positions_by_job_id == {"job-queued": 1}
    assert snapshot.retry_waiting_job_ids == {"job-retry"}
    assert snapshot.retry_after_by_job_id == {"job-retry": 45}
    assert snapshot.queue_length == 1
    assert snapshot.retry_waiting_count == 1
    assert snapshot.max_concurrent_recordings == 2
    assert snapshot.available_slots == 0

    response = snapshot.to_active_response()
    assert response["active"] is True
    assert response["active_count"] == 1
    assert response["active_jobs"][0]["job_id"] == "job-recording"
    assert response["queued_items"][0]["meeting_code"] == "room-queued"
    assert response["retry_waiting_items"][0]["meeting_code"] == "room-retry"


def test_runtime_snapshot_derives_available_slots_when_runner_field_missing(db_session):
    db_session.add(_job("job-recording", JobStatus.RECORDING.value, "room-recording"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[SimpleNamespace(job_id="job-recording")])
    runner = SimpleNamespace(
        queued_items=[],
        retry_waiting_items=[],
        max_concurrent_recordings=3,
    )

    snapshot = JobRuntimeStateService().build_snapshot(db_session, worker=worker, runner=runner)

    assert snapshot.max_concurrent_recordings == 3
    assert snapshot.available_slots == 2


def test_runtime_snapshot_derives_available_slots_when_runner_field_invalid(db_session):
    db_session.add(_job("job-recording", JobStatus.RECORDING.value, "room-recording"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[SimpleNamespace(job_id="job-recording")])
    runner = SimpleNamespace(
        queued_items=[],
        retry_waiting_items=[],
        max_concurrent_recordings=3,
        available_slots=None,
    )

    snapshot = JobRuntimeStateService().build_snapshot(db_session, worker=worker, runner=runner)

    assert snapshot.max_concurrent_recordings == 3
    assert snapshot.available_slots == 2


@pytest.mark.parametrize("queue_length", [-1, "invalid"])
def test_runtime_snapshot_derives_fifo_queue_length_from_items_when_runner_count_invalid(db_session, queue_length):
    db_session.add_all(
        [
            _job("job-queued-a", JobStatus.QUEUED.value, "room-a"),
            _job("job-queued-b", JobStatus.QUEUED.value, "room-b"),
        ]
    )
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[])
    runner = SimpleNamespace(
        queued_items=[
            _queued_item("immediate", 1, job_id="job-queued-a"),
            _queued_item("immediate", 2, job_id="job-queued-b"),
        ],
        queue_length=queue_length,
        retry_waiting_items=[],
        max_concurrent_recordings=2,
        available_slots=2,
    )

    snapshot = JobRuntimeStateService().build_snapshot(db_session, worker=worker, runner=runner)

    assert snapshot.queue_length == 2


@pytest.mark.parametrize("retry_waiting_count", [-1, "invalid"])
def test_runtime_snapshot_derives_retry_waiting_count_from_items_when_runner_count_invalid(
    db_session, retry_waiting_count
):
    db_session.add_all(
        [
            _job("job-retry-a", JobStatus.QUEUED.value, "room-a"),
            _job("job-retry-b", JobStatus.QUEUED.value, "room-b"),
        ]
    )
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[])
    runner = SimpleNamespace(
        queued_items=[],
        retry_waiting_items=[
            _retry_item("job-retry-a", retry_after_sec=30),
            _retry_item("job-retry-b", retry_after_sec=60),
        ],
        retry_waiting_count=retry_waiting_count,
        queue_length=0,
        max_concurrent_recordings=2,
        available_slots=2,
    )

    snapshot = JobRuntimeStateService().build_snapshot(db_session, worker=worker, runner=runner)

    assert snapshot.retry_waiting_count == 2


@pytest.mark.parametrize("max_concurrent_recordings", [None, "invalid"])
def test_runtime_snapshot_falls_back_when_runner_max_concurrent_invalid(db_session, max_concurrent_recordings):
    worker = SimpleNamespace(active_jobs=[])
    runner = SimpleNamespace(
        queued_items=[],
        retry_waiting_items=[],
        max_concurrent_recordings=max_concurrent_recordings,
    )

    snapshot = JobRuntimeStateService().build_snapshot(db_session, worker=worker, runner=runner)

    assert snapshot.max_concurrent_recordings == 1
    assert snapshot.available_slots == 0
