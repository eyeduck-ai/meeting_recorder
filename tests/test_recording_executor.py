"""Tests for recording execution retry and status flow."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scheduling.recording_executor as executor_module
from database.migrations import run_schema_migrations
from database.models import Base, JobStatus
from database.models import RecordingJob as RecordingJobModel
from database.session import JobRepository
from recording.ffmpeg_pipeline import RecordingInfo
from recording.worker import RecordingJob, RecordingResult
from scheduling.recording_executor import RecordingExecutor
from utils.timezone import utc_now


class FakeWorker:
    """Worker stub that returns deterministic recording results."""

    def __init__(self, results: list[RecordingResult]):
        self._results = list(results)
        self._status_callback = None
        self.calls: list[tuple[str, int]] = []

    def set_status_callback(self, callback) -> None:
        self._status_callback = callback

    async def record(self, job: RecordingJob) -> RecordingResult:
        self.calls.append((job.job_id, job.attempt_no))
        if self._status_callback:
            self._status_callback(job.job_id, JobStatus.STARTING)

        result = self._results.pop(0)
        if result.status == JobStatus.SUCCEEDED and self._status_callback:
            self._status_callback(job.job_id, JobStatus.RECORDING)
        return result


@pytest.fixture
def executor_session_local(tmp_path, monkeypatch):
    """Provide an isolated DB and patched executor dependencies."""
    db_path = tmp_path / "recording-executor.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(executor_module, "get_session_local", lambda: SessionLocal)
    monkeypatch.setattr(executor_module, "notify_recording_completed", AsyncMock())
    monkeypatch.setattr(executor_module, "notify_recording_failed", AsyncMock())
    monkeypatch.setattr(executor_module, "notify_recording_retry", AsyncMock())
    monkeypatch.setattr(executor_module, "notify_recording_status", AsyncMock(return_value=None))
    monkeypatch.setattr(executor_module, "ACTIVE_NOTIFICATION_STATUSES", set())

    return SessionLocal


def _create_db_job(session_local, job: RecordingJob) -> None:
    session = session_local()
    try:
        JobRepository(session).create(
            job_id=job.job_id,
            schedule_id=None,
            provider=job.provider,
            meeting_code=job.meeting_code,
            display_name=job.display_name,
            base_url=job.base_url,
            duration_sec=job.duration_sec,
            lobby_wait_sec=job.lobby_wait_sec,
            status=JobStatus.QUEUED.value,
            attempt_no=job.attempt_no,
            retry_count=max(0, job.attempt_no - 1),
        )
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_retryable_failure_retries_same_job_row(executor_session_local, monkeypatch, tmp_path):
    """Retryable errors should reuse the same job id and update one DB row."""

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(executor_module.asyncio, "sleep", fast_sleep)

    job = RecordingJob(
        job_id="retry123",
        provider="jitsi",
        meeting_code="room-123",
        display_name="Recorder Bot",
        duration_sec=300,
        output_dir=tmp_path,
    )
    _create_db_job(executor_session_local, job)

    fake_worker = FakeWorker(
        [
            RecordingResult(
                job_id=job.job_id,
                status=JobStatus.FAILED,
                attempt_no=1,
                error_message="ERR_NAME_NOT_RESOLVED while joining",
                end_time=utc_now(),
            ),
            RecordingResult(job_id=job.job_id, status=JobStatus.SUCCEEDED, attempt_no=2, end_time=utc_now()),
        ]
    )
    executor = RecordingExecutor(worker_provider=lambda: fake_worker)

    upload_request = await executor.run_with_retry(
        job=job,
        schedule_id=None,
        meeting_end_time=utc_now() + timedelta(minutes=5),
        youtube_enabled=False,
        youtube_privacy="unlisted",
        meeting_name=None,
    )

    assert upload_request is None
    assert fake_worker.calls == [("retry123", 1), ("retry123", 2)]

    session = executor_session_local()
    try:
        rows = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "retry123").all()
        assert len(rows) == 1
        assert rows[0].status == JobStatus.SUCCEEDED.value
        assert rows[0].attempt_no == 2
        assert rows[0].retry_count == 1
    finally:
        session.close()


@pytest.mark.asyncio
async def test_success_with_youtube_enabled_returns_upload_request(executor_session_local, tmp_path):
    """Successful YouTube-enabled recordings should return an upload request."""
    output_path = tmp_path / "recording.mkv"
    output_path.write_bytes(b"video")
    now = utc_now()
    job = RecordingJob(
        job_id="upload123",
        provider="zoom",
        meeting_code="https://zoom.us/j/123",
        display_name="Recorder Bot",
        duration_sec=300,
        output_dir=tmp_path,
    )
    _create_db_job(executor_session_local, job)

    fake_worker = FakeWorker(
        [
            RecordingResult(
                job_id=job.job_id,
                status=JobStatus.SUCCEEDED,
                attempt_no=1,
                recording_info=RecordingInfo(
                    output_path=output_path,
                    file_size=output_path.stat().st_size,
                    duration_sec=60.0,
                    start_time=now,
                    end_time=now + timedelta(seconds=60),
                ),
                recording_started_at=now,
                end_time=now + timedelta(seconds=60),
            )
        ]
    )
    executor = RecordingExecutor(worker_provider=lambda: fake_worker)

    upload_request = await executor.run_with_retry(
        job=job,
        schedule_id=123,
        meeting_end_time=utc_now() + timedelta(minutes=5),
        youtube_enabled=True,
        youtube_privacy="private",
        meeting_name="Weekly Review",
    )

    assert upload_request is not None
    assert upload_request.job_id == "upload123"
    assert upload_request.video_path == output_path
    assert upload_request.privacy == "private"
    assert "Weekly Review" in upload_request.title
    assert "https://zoom.us/j/123" in upload_request.title

    session = executor_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "upload123").first()
        assert db_job.status == JobStatus.SUCCEEDED.value
        assert db_job.youtube_enabled is True
    finally:
        session.close()
