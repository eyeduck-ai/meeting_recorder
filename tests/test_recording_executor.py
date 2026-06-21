"""Tests for recording execution retry and status flow."""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scheduling.recording_executor as executor_module
from database.migrations import run_schema_migrations
from database.models import Base, JobStatus
from database.models import RecordingJob as RecordingJobModel
from database.session import JobRepository
from recording.ffmpeg_pipeline import RecordingInfo
from recording.job_types import RecordingJob, RecordingResult
from recording.post_processing import RecordingPostProcessingRequest
from scheduling.recording_executor import RecordingExecutor, RecordingRetryRequest
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


def _create_db_job_with_status(session_local, *, job_id: str, status: JobStatus) -> None:
    session = session_local()
    try:
        session.add(
            RecordingJobModel(
                job_id=job_id,
                provider="jitsi",
                meeting_code="room-123",
                display_name="Recorder Bot",
                duration_sec=300,
                lobby_wait_sec=900,
                status=status.value,
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_stage_notification_skips_stale_db_status(executor_session_local, monkeypatch):
    """A late stage notification should not overwrite or send after the job has moved on."""
    _create_db_job_with_status(executor_session_local, job_id="stage-stale", status=JobStatus.SUCCEEDED)
    notify = AsyncMock(return_value=123)
    monkeypatch.setattr(executor_module, "notify_recording_status", notify)
    executor = RecordingExecutor(worker_provider=lambda: FakeWorker([]))

    await executor._notify_stage_update("stage-stale", JobStatus.RECORDING)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_stage_notification_skips_stale_finalizing_db_status(executor_session_local, monkeypatch):
    """A finalizing notification should not be sent after the job has already completed."""
    _create_db_job_with_status(executor_session_local, job_id="stage-finalizing-stale", status=JobStatus.SUCCEEDED)
    notify = AsyncMock(return_value=123)
    monkeypatch.setattr(executor_module, "notify_recording_status", notify)
    executor = RecordingExecutor(worker_provider=lambda: FakeWorker([]))

    await executor._notify_stage_update("stage-finalizing-stale", JobStatus.FINALIZING)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_stage_notification_sends_when_db_status_still_matches(executor_session_local, monkeypatch):
    """A current stage notification should be sent and persist the Telegram message id."""
    _create_db_job_with_status(executor_session_local, job_id="stage-current", status=JobStatus.RECORDING)
    notify = AsyncMock(return_value=321)
    monkeypatch.setattr(executor_module, "notify_recording_status", notify)
    executor = RecordingExecutor(worker_provider=lambda: FakeWorker([]))

    await executor._notify_stage_update("stage-current", JobStatus.RECORDING)

    notify.assert_awaited_once()
    session = executor_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "stage-current").one()
        assert db_job.telegram_message_id == 321
    finally:
        session.close()


@pytest.mark.asyncio
async def test_stage_notification_failure_is_best_effort(executor_session_local, monkeypatch, caplog):
    """Notification failure should warn without raising or mutating the job result."""
    _create_db_job_with_status(executor_session_local, job_id="stage-failure", status=JobStatus.RECORDING)
    notify = AsyncMock(side_effect=RuntimeError("telegram unavailable"))
    monkeypatch.setattr(executor_module, "notify_recording_status", notify)
    executor = RecordingExecutor(worker_provider=lambda: FakeWorker([]))

    with caplog.at_level(logging.WARNING):
        await executor._notify_stage_update("stage-failure", JobStatus.RECORDING)

    notify.assert_awaited_once()
    assert "Failed to send stage notification for job stage-failure" in caplog.text
    session = executor_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "stage-failure").one()
        assert db_job.status == JobStatus.RECORDING.value
        assert db_job.telegram_message_id is None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_successful_raw_capture_schedules_finalizing_notification(executor_session_local, tmp_path):
    """Successful raw captures should expose the finalizing stage before post-processing."""
    output_path = tmp_path / "recording.mkv"
    output_path.write_bytes(b"video")
    now = utc_now()
    job = RecordingJob(
        job_id="finalizing-stage",
        provider="jitsi",
        meeting_code="room",
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
                end_time=now + timedelta(seconds=60),
            )
        ]
    )
    executor = RecordingExecutor(worker_provider=lambda: fake_worker)
    schedule_stage_notification = Mock()
    executor._schedule_stage_notification = schedule_stage_notification

    await executor.run_with_retry(
        job=job,
        schedule_id=None,
        meeting_end_time=utc_now() + timedelta(minutes=5),
        youtube_enabled=False,
        youtube_privacy="unlisted",
        meeting_name=None,
    )

    schedule_stage_notification.assert_called_once_with("finalizing-stage", JobStatus.FINALIZING)


@pytest.mark.asyncio
async def test_retryable_failure_returns_delayed_retry_request(executor_session_local, tmp_path):
    """Retryable errors should persist the attempt and ask the runner to requeue later."""
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

    retry_request = await executor.run_with_retry(
        job=job,
        schedule_id=None,
        meeting_end_time=utc_now() + timedelta(minutes=5),
        youtube_enabled=False,
        youtube_privacy="unlisted",
        meeting_name=None,
    )

    assert isinstance(retry_request, RecordingRetryRequest)
    assert retry_request.job.job_id == "retry123"
    assert retry_request.job.attempt_no == 2
    assert retry_request.delay_sec == 15
    assert retry_request.next_retry_delay_sec == 30
    assert fake_worker.calls == [("retry123", 1)]

    session = executor_session_local()
    try:
        rows = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "retry123").all()
        assert len(rows) == 1
        assert rows[0].status == JobStatus.QUEUED.value
        assert rows[0].attempt_no == 1
        assert rows[0].retry_count == 0
        assert rows[0].completed_at is None
        assert "Retry scheduled" in rows[0].error_message
    finally:
        session.close()

    final_outcome = await executor.run_with_retry(
        job=retry_request.job,
        schedule_id=retry_request.schedule_id,
        meeting_end_time=retry_request.meeting_end_time,
        youtube_enabled=retry_request.youtube_enabled,
        youtube_privacy=retry_request.youtube_privacy,
        meeting_name=retry_request.meeting_name,
        retry_delay_sec=retry_request.next_retry_delay_sec,
    )

    assert final_outcome is None
    assert fake_worker.calls == [("retry123", 1), ("retry123", 2)]

    session = executor_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "retry123").one()
        assert db_job.status == JobStatus.SUCCEEDED.value
        assert db_job.attempt_no == 2
        assert db_job.retry_count == 1
    finally:
        session.close()


@pytest.mark.asyncio
async def test_retry_request_sets_hard_deadline_without_double_counting_dynamic_extension(
    executor_session_local, tmp_path
):
    job = RecordingJob(
        job_id="retry-hard-deadline",
        provider="jitsi",
        meeting_code="room-123",
        display_name="Recorder Bot",
        duration_sec=120,
        output_dir=tmp_path,
        dynamic_extension_enabled=True,
        dynamic_extension_max_sec=60,
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
            )
        ]
    )
    executor = RecordingExecutor(worker_provider=lambda: fake_worker)
    meeting_end_time = utc_now() + timedelta(seconds=180)

    retry_request = await executor.run_with_retry(
        job=job,
        schedule_id=None,
        meeting_end_time=meeting_end_time,
        youtube_enabled=False,
        youtube_privacy="unlisted",
        meeting_name=None,
    )

    assert isinstance(retry_request, RecordingRetryRequest)
    assert retry_request.job.hard_deadline_at == meeting_end_time
    assert 1 <= retry_request.job.duration_sec <= 120


@pytest.mark.asyncio
async def test_retry_request_clears_expired_fixed_deadline_for_extension_window(executor_session_local, tmp_path):
    job = RecordingJob(
        job_id="retry-expired-base",
        provider="jitsi",
        meeting_code="room-123",
        display_name="Recorder Bot",
        duration_sec=120,
        output_dir=tmp_path,
        deadline_at=utc_now() - timedelta(seconds=5),
        dynamic_extension_enabled=True,
        dynamic_extension_max_sec=60,
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
            )
        ]
    )
    executor = RecordingExecutor(worker_provider=lambda: fake_worker)
    meeting_end_time = utc_now() + timedelta(seconds=50)

    retry_request = await executor.run_with_retry(
        job=job,
        schedule_id=10,
        meeting_end_time=meeting_end_time,
        youtube_enabled=False,
        youtube_privacy="unlisted",
        meeting_name=None,
    )

    assert isinstance(retry_request, RecordingRetryRequest)
    assert retry_request.job.deadline_at is None
    assert retry_request.job.hard_deadline_at == meeting_end_time
    assert retry_request.job.duration_sec == 1


@pytest.mark.asyncio
async def test_success_with_youtube_enabled_returns_post_processing_request(executor_session_local, tmp_path):
    """Successful raw captures should leave capacity and continue in post-processing."""
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

    outcome = await executor.run_with_retry(
        job=job,
        schedule_id=123,
        meeting_end_time=utc_now() + timedelta(minutes=5),
        youtube_enabled=True,
        youtube_privacy="private",
        meeting_name="Weekly Review",
    )

    assert isinstance(outcome, RecordingPostProcessingRequest)
    assert outcome.job.job_id == "upload123"
    assert outcome.result.recording_info.output_path == output_path
    assert outcome.youtube_enabled is True
    assert outcome.youtube_privacy == "private"
    assert outcome.meeting_name == "Weekly Review"

    session = executor_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "upload123").first()
        assert db_job.status == JobStatus.FINALIZING.value
        assert db_job.output_path == str(output_path)
        assert db_job.raw_output_path == str(output_path)
        assert db_job.completed_at is None
        assert db_job.youtube_enabled is True
    finally:
        session.close()
