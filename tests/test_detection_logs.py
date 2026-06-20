"""Tests for activity/detection log routes."""

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.detection import export_detection_logs, get_detection_logs, router
from database.models import Base, DetectionLog, JobStatus, RecordingJob
from database.session import get_db
from utils.timezone import utc_now


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'detection-logs.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _create_job(db_session):
    job = RecordingJob(
        job_id="activity-log-job",
        provider="jitsi",
        meeting_code="room",
        display_name="Recorder Bot",
        duration_sec=3600,
        status=JobStatus.SUCCEEDED.value,
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(router)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.mark.asyncio
async def test_detection_logs_filter_by_detector_type_and_detected(db_session):
    job = _create_job(db_session)
    db_session.add_all(
        [
            DetectionLog(
                job_id=job.id,
                detector_type="media_activity",
                detected=True,
                reason="trimmed",
                triggered_at=utc_now(),
            ),
            DetectionLog(
                job_id=job.id,
                detector_type="media_activity",
                detected=False,
                reason="no trim",
                triggered_at=utc_now() - timedelta(seconds=1),
            ),
            DetectionLog(
                job_id=job.id,
                detector_type="dynamic_extension",
                detected=True,
                reason="idle_timeout",
                triggered_at=utc_now() - timedelta(seconds=2),
            ),
        ]
    )
    db_session.commit()

    response = await get_detection_logs(
        db=db_session,
        job_id=None,
        detector_type="media_activity",
        detected=True,
        limit=20,
        offset=0,
    )
    payload = json.loads(response.body)

    assert payload["total"] == 1
    assert payload["summary"] == {"triggered": 1, "accurate": 0, "inaccurate": 0}
    assert len(payload["logs"]) == 1
    assert payload["logs"][0]["detector_type"] == "media_activity"
    assert payload["logs"][0]["detected"] is True


@pytest.mark.asyncio
async def test_detection_logs_summary_uses_filtered_total_not_page(db_session):
    job = _create_job(db_session)
    db_session.add_all(
        [
            DetectionLog(
                job_id=job.id,
                detector_type="media_activity",
                detected=True,
                was_accurate=True,
                triggered_at=utc_now(),
            ),
            DetectionLog(
                job_id=job.id,
                detector_type="media_activity",
                detected=False,
                was_accurate=False,
                triggered_at=utc_now() - timedelta(seconds=1),
            ),
            DetectionLog(
                job_id=job.id,
                detector_type="dynamic_extension",
                detected=True,
                was_accurate=False,
                triggered_at=utc_now() - timedelta(seconds=2),
            ),
        ]
    )
    db_session.commit()

    response = await get_detection_logs(
        db=db_session,
        job_id=None,
        detector_type="media_activity",
        detected=None,
        limit=1,
        offset=0,
    )
    payload = json.loads(response.body)

    assert payload["total"] == 2
    assert len(payload["logs"]) == 1
    assert payload["summary"] == {"triggered": 1, "accurate": 1, "inaccurate": 1}


def test_detection_logs_reject_invalid_pagination(client):
    assert client.get("/api/detection/logs?limit=0").status_code == 422
    assert client.get("/api/detection/logs?offset=-1").status_code == 422


@pytest.mark.asyncio
async def test_detection_logs_export_uses_same_filters(db_session):
    job = _create_job(db_session)
    other_job = RecordingJob(
        job_id="other-activity-log-job",
        provider="jitsi",
        meeting_code="other-room",
        display_name="Recorder Bot",
        duration_sec=3600,
        status=JobStatus.SUCCEEDED.value,
    )
    db_session.add(other_job)
    db_session.commit()
    db_session.add_all(
        [
            DetectionLog(job_id=job.id, detector_type="media_activity", detected=True),
            DetectionLog(job_id=job.id, detector_type="dynamic_extension", detected=True),
            DetectionLog(job_id=other_job.id, detector_type="dynamic_extension", detected=True),
        ]
    )
    db_session.commit()

    response = await export_detection_logs(
        db=db_session,
        job_id=job.id,
        detector_type="dynamic_extension",
        detected=True,
        format="json",
    )
    payload = json.loads(response.body)

    assert len(payload) == 1
    assert payload[0]["detector_type"] == "dynamic_extension"
    assert payload[0]["job_id"] == job.id
