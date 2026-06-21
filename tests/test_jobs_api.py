from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.routes.ui_dashboard as ui_dashboard_module
import api.routes.ui_jobs as ui_jobs_module
from api.routes.jobs import finish_job, get_active_recordings, get_current_recording, stop_job
from api.routes.ui_jobs import (
    jobs_delete as ui_jobs_delete,
)
from api.routes.ui_jobs import (
    jobs_delete_all as ui_jobs_delete_all,
)
from api.routes.ui_jobs import (
    jobs_finish as ui_jobs_finish,
)
from api.routes.ui_jobs import (
    jobs_stop as ui_jobs_stop,
)
from database.migrations import run_schema_migrations
from database.models import Base, ErrorCode, JobStatus
from database.models import RecordingJob as RecordingJobModel
from utils.timezone import utc_now


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "jobs-api.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _request(worker, runner):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=worker, job_runner=runner)))


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
        status=JobStatus.QUEUED.value,
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


def _capture_render_template(_request, name: str, **kwargs):
    return {"template": name, **kwargs}


@pytest.mark.asyncio
async def test_jobs_active_returns_all_worker_active_jobs_and_capacity(db_session):
    db_session.add(_job("job-a", JobStatus.RECORDING.value, "room-a"))
    db_session.add(_job("job-b", JobStatus.JOINING.value, "room-b"))
    db_session.commit()
    worker = SimpleNamespace(
        active_jobs=[SimpleNamespace(job_id="job-a"), SimpleNamespace(job_id="job-b")],
        is_busy=True,
    )
    runner = SimpleNamespace(queue_length=3, max_concurrent_recordings=2, available_slots=0)

    response = await get_active_recordings(_request(worker, runner), db_session)

    assert response["active"] is True
    assert response["active_count"] == 2
    assert response["queue_length"] == 3
    assert response["max_concurrent_recordings"] == 2
    assert {job["job_id"] for job in response["active_jobs"]} == {"job-a", "job-b"}


@pytest.mark.asyncio
async def test_jobs_active_and_current_filter_worker_registry_to_active_statuses(db_session):
    db_session.add_all(
        [
            _job("job-recording", JobStatus.RECORDING.value, "room-recording"),
            _job("job-uploading", JobStatus.UPLOADING.value, "room-uploading"),
            _job("job-succeeded", JobStatus.SUCCEEDED.value, "room-succeeded"),
        ]
    )
    db_session.commit()
    worker = SimpleNamespace(
        active_jobs=[
            SimpleNamespace(job_id="job-recording"),
            SimpleNamespace(job_id="job-uploading"),
            SimpleNamespace(job_id="job-succeeded"),
        ],
        is_busy=True,
    )
    runner = SimpleNamespace(queue_length=0, max_concurrent_recordings=2, available_slots=1)

    active_response = await get_active_recordings(_request(worker, runner), db_session)
    current_response = await get_current_recording(_request(worker, runner), db_session)

    assert active_response["active"] is True
    assert active_response["active_count"] == 1
    assert [job["job_id"] for job in active_response["active_jobs"]] == ["job-recording"]
    assert current_response["active"] is True
    assert current_response["active_count"] == 1
    assert current_response["job"]["job_id"] == "job-recording"


@pytest.mark.asyncio
async def test_jobs_active_returns_queued_items(db_session):
    db_session.add(_job("job-q", JobStatus.QUEUED.value, "room-q"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[], is_busy=False)
    runner = SimpleNamespace(
        queue_length=1,
        max_concurrent_recordings=2,
        available_slots=1,
        queued_items=[_queued_item("immediate", 1, job_id="job-q")],
    )

    response = await get_active_recordings(_request(worker, runner), db_session)

    assert response["active_count"] == 0
    assert response["queue_length"] == 1
    assert response["queued_items"] == [
        {
            "kind": "immediate",
            "queue_position": 1,
            "job_id": "job-q",
            "schedule_id": None,
            "status": "queued",
            "meeting_code": "room-q",
            "display_name": "Recorder Bot",
            "manual_trigger": False,
            "created_at": response["queued_items"][0]["created_at"],
        }
    ]


@pytest.mark.asyncio
async def test_jobs_active_returns_retry_waiting_items_without_changing_queue_length(db_session):
    db_session.add(_job("job-r", JobStatus.QUEUED.value, "room-r"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[], is_busy=False)
    runner = SimpleNamespace(
        queue_length=0,
        max_concurrent_recordings=2,
        available_slots=2,
        queued_items=[],
        retry_waiting_items=[_retry_item("job-r", schedule_id=7, retry_after_sec=45)],
        retry_waiting_count=1,
    )

    response = await get_active_recordings(_request(worker, runner), db_session)

    assert response["queue_length"] == 0
    assert response["retry_waiting_count"] == 1
    assert response["retry_waiting_items"] == [
        {
            "job_id": "job-r",
            "schedule_id": 7,
            "status": "retry_waiting",
            "retry_after_sec": 45,
            "meeting_code": "room-r",
            "display_name": "Recorder Bot",
        }
    ]


@pytest.mark.asyncio
async def test_jobs_current_keeps_compat_shape_with_active_count(db_session):
    db_session.add(_job("job-a", JobStatus.RECORDING.value, "room-a"))
    db_session.add(_job("job-b", JobStatus.JOINING.value, "room-b"))
    db_session.commit()
    worker = SimpleNamespace(
        active_jobs=[SimpleNamespace(job_id="job-a"), SimpleNamespace(job_id="job-b")],
        is_busy=True,
    )
    runner = SimpleNamespace(queue_length=0, max_concurrent_recordings=2, available_slots=0)

    response = await get_current_recording(_request(worker, runner), db_session)

    assert response["active"] is True
    assert response["active_count"] == 2
    assert response["job"]["job_id"] in {"job-a", "job-b"}


@pytest.mark.asyncio
async def test_jobs_current_ignores_finalizing_job_outside_worker_registry(db_session):
    db_session.add(_job("job-finalizing", JobStatus.FINALIZING.value, "room-finalizing"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[], is_busy=False)
    runner = SimpleNamespace(queue_length=0, max_concurrent_recordings=2, available_slots=2)

    active_response = await get_active_recordings(_request(worker, runner), db_session)
    current_response = await get_current_recording(_request(worker, runner), db_session)

    assert active_response["active"] is False
    assert active_response["active_count"] == 0
    assert active_response["active_jobs"] == []
    assert current_response["active"] is False


@pytest.mark.asyncio
async def test_jobs_current_ignores_stale_private_current_job_fallback(db_session):
    db_session.add(_job("job-stale", JobStatus.RECORDING.value, "room-stale"))
    db_session.commit()
    worker = SimpleNamespace(
        active_jobs=[],
        is_busy=True,
        _current_job=SimpleNamespace(job_id="job-stale"),
    )
    runner = SimpleNamespace(queue_length=0, max_concurrent_recordings=2, available_slots=2)

    response = await get_current_recording(_request(worker, runner), db_session)

    assert response["active"] is False
    assert response["active_count"] == 0
    assert response["job"] is None


@pytest.mark.asyncio
async def test_stop_job_cancels_queued_immediate_job(db_session):
    db_session.add(_job("job-q", JobStatus.QUEUED.value, "room-q"))
    db_session.commit()
    worker = SimpleNamespace(is_job_active=lambda _job_id: False)

    class FakeRunner:
        queue_length = 1
        max_concurrent_recordings = 2
        available_slots = 0

        def cancel_queued_job(self, job_id: str) -> bool:
            return job_id == "job-q"

    response = await stop_job("job-q", _request(worker, FakeRunner()), db_session)

    db_session.expire_all()
    db_job = db_session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "job-q").one()
    assert response == {"message": "Queued job canceled", "job_id": "job-q"}
    assert db_job.status == JobStatus.CANCELED.value
    assert db_job.error_code == ErrorCode.CANCELED.value
    assert db_job.end_reason == "canceled"
    assert db_job.completed_at is not None


@pytest.mark.asyncio
async def test_stop_job_cancels_retry_waiting_job(db_session):
    db_session.add(_job("job-r", JobStatus.QUEUED.value, "room-r"))
    db_session.commit()
    worker = SimpleNamespace(is_job_active=lambda _job_id: False)

    class FakeRunner:
        def cancel_queued_job_for_action(self, job_id: str):
            return SimpleNamespace(removed=job_id == "job-r", source="retry_waiting", schedule_id=None)

        def is_retry_waiting_job(self, job_id: str) -> bool:
            raise AssertionError("structured cancel should not pre-check retry state")

    response = await stop_job("job-r", _request(worker, FakeRunner()), db_session)

    db_session.expire_all()
    db_job = db_session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "job-r").one()
    assert response == {"message": "Retry waiting job canceled", "job_id": "job-r"}
    assert db_job.status == JobStatus.CANCELED.value
    assert db_job.error_message == "Canceled while waiting to retry"


@pytest.mark.asyncio
async def test_finish_job_rejects_queued_job(db_session):
    db_session.add(_job("job-q", JobStatus.QUEUED.value, "room-q"))
    db_session.commit()
    worker = SimpleNamespace(is_job_active=lambda _job_id: False)
    runner = SimpleNamespace(queue_length=1, max_concurrent_recordings=2, available_slots=0)

    with pytest.raises(HTTPException) as exc_info:
        await finish_job("job-q", _request(worker, runner), db_session)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Queued jobs cannot be finished"


@pytest.mark.asyncio
async def test_ui_stop_cancels_queued_immediate_job(db_session):
    db_session.add(_job("job-q", JobStatus.QUEUED.value, "room-q"))
    db_session.commit()
    worker = SimpleNamespace(is_job_active=lambda _job_id: False)

    class FakeRunner:
        def __init__(self):
            self.canceled = []

        def cancel_queued_job(self, job_id: str) -> bool:
            self.canceled.append(job_id)
            return job_id == "job-q"

    runner = FakeRunner()
    response = await ui_jobs_stop(_request(worker, runner), "job-q", db_session)

    db_session.expire_all()
    db_job = db_session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "job-q").one()
    assert response.status_code == 303
    assert runner.canceled == ["job-q"]
    assert db_job.status == JobStatus.CANCELED.value
    assert db_job.error_code == ErrorCode.CANCELED.value
    assert db_job.end_reason == "canceled"


@pytest.mark.asyncio
async def test_ui_finish_rejects_queued_job(db_session):
    db_session.add(_job("job-q", JobStatus.QUEUED.value, "room-q"))
    db_session.commit()
    worker = SimpleNamespace(is_job_active=lambda _job_id: False)
    runner = SimpleNamespace()

    with pytest.raises(HTTPException) as exc_info:
        await ui_jobs_finish(_request(worker, runner), "job-q", db_session)

    db_session.expire_all()
    db_job = db_session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "job-q").one()
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Queued jobs cannot be finished"
    assert db_job.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_ui_jobs_active_filter_includes_queued_and_uploading_but_controls_only_active_statuses(
    db_session, monkeypatch
):
    db_session.add_all(
        [
            _job("job-queued", JobStatus.QUEUED.value, "room-queued"),
            _job("job-retry", JobStatus.QUEUED.value, "room-retry"),
            _job("job-recording", JobStatus.RECORDING.value, "room-recording"),
            _job("job-uploading", JobStatus.UPLOADING.value, "room-uploading"),
            _job("job-succeeded", JobStatus.SUCCEEDED.value, "room-succeeded"),
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
        retry_waiting_items=[_retry_item("job-retry", retry_after_sec=30)],
    )
    monkeypatch.setattr(ui_jobs_module.ui_common, "render_template", _capture_render_template)

    response = await ui_jobs_module.jobs_list(_request(worker, runner), "active", db_session)

    returned_job_ids = {job.job_id for job in response["jobs"]}
    assert response["template"] == "jobs/list.html"
    assert returned_job_ids == {"job-queued", "job-retry", "job-recording", "job-uploading"}
    assert response["active_job_ids"] == {"job-recording"}
    assert response["queued_job_ids"] == {"job-queued"}
    assert response["retry_waiting_job_ids"] == {"job-retry"}


@pytest.mark.asyncio
async def test_ui_dashboard_filters_worker_registry_to_active_recording_statuses(db_session, monkeypatch):
    db_session.add_all(
        [
            _job("job-recording", JobStatus.RECORDING.value, "room-recording"),
            _job("job-uploading", JobStatus.UPLOADING.value, "room-uploading"),
            _job("job-succeeded", JobStatus.SUCCEEDED.value, "room-succeeded"),
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
        queued_items=[],
        queue_length=0,
        max_concurrent_recordings=2,
        available_slots=1,
    )
    monkeypatch.setattr(ui_dashboard_module.ui_common, "render_template", _capture_render_template)

    response = await ui_dashboard_module.dashboard(_request(worker, runner), db_session)

    assert response["template"] == "dashboard.html"
    assert [job.job_id for job in response["active_jobs"]] == ["job-recording"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.UPLOADING.value, JobStatus.SUCCEEDED.value])
async def test_ui_jobs_detail_does_not_allow_controls_for_non_recording_status_in_worker_registry(
    db_session, monkeypatch, status
):
    db_session.add(_job("job-raced", status, "room-raced"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[SimpleNamespace(job_id="job-raced")])
    runner = SimpleNamespace(queued_items=[])
    monkeypatch.setattr(ui_jobs_module.ui_common, "render_template", _capture_render_template)

    response = await ui_jobs_module.jobs_detail(_request(worker, runner), "job-raced", db_session)

    assert response["template"] == "jobs/detail.html"
    assert response["can_control_job"] is False


@pytest.mark.asyncio
async def test_ui_jobs_detail_does_not_allow_controls_for_finalizing_outside_worker_registry(db_session, monkeypatch):
    db_session.add(_job("job-finalizing", JobStatus.FINALIZING.value, "room-finalizing"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[])
    runner = SimpleNamespace(queued_items=[])
    monkeypatch.setattr(ui_jobs_module.ui_common, "render_template", _capture_render_template)

    response = await ui_jobs_module.jobs_detail(_request(worker, runner), "job-finalizing", db_session)

    assert response["template"] == "jobs/detail.html"
    assert response["can_control_job"] is False
    assert response["can_delete_job"] is False


@pytest.mark.asyncio
async def test_ui_jobs_detail_marks_retry_waiting_job_cancelable(db_session, monkeypatch):
    db_session.add(_job("job-retry", JobStatus.QUEUED.value, "room-retry"))
    db_session.commit()
    worker = SimpleNamespace(active_jobs=[])
    runner = SimpleNamespace(queued_items=[], retry_waiting_items=[_retry_item("job-retry", retry_after_sec=25)])
    monkeypatch.setattr(ui_jobs_module.ui_common, "render_template", _capture_render_template)

    response = await ui_jobs_module.jobs_detail(_request(worker, runner), "job-retry", db_session)

    assert response["template"] == "jobs/detail.html"
    assert response["can_control_job"] is False
    assert response["is_retry_waiting_job"] is True
    assert response["retry_after_sec"] == 25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        JobStatus.QUEUED.value,
        JobStatus.RECORDING.value,
        JobStatus.UPLOADING.value,
    ],
)
async def test_ui_delete_rejects_non_terminal_jobs(db_session, status):
    db_session.add(_job("job-live", status, "room-live"))
    db_session.commit()
    worker = SimpleNamespace(is_job_active=lambda _job_id: status == JobStatus.RECORDING.value)
    runner = SimpleNamespace()

    with pytest.raises(HTTPException) as exc_info:
        await ui_jobs_delete(_request(worker, runner), "job-live", db_session)

    assert exc_info.value.status_code == 400
    assert db_session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "job-live").one()


@pytest.mark.asyncio
async def test_ui_delete_all_only_deletes_terminal_jobs(db_session):
    db_session.add_all(
        [
            _job("job-succeeded", JobStatus.SUCCEEDED.value, "room-s"),
            _job("job-failed", JobStatus.FAILED.value, "room-f"),
            _job("job-canceled", JobStatus.CANCELED.value, "room-c"),
            _job("job-queued", JobStatus.QUEUED.value, "room-q"),
            _job("job-uploading", JobStatus.UPLOADING.value, "room-u"),
        ]
    )
    db_session.commit()
    worker = SimpleNamespace()
    runner = SimpleNamespace()

    response = await ui_jobs_delete_all(_request(worker, runner), db_session)

    remaining_ids = {job.job_id for job in db_session.query(RecordingJobModel).all()}
    assert response.status_code == 200
    assert remaining_ids == {"job-queued", "job-uploading"}
