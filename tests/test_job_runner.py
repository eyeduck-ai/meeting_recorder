"""Tests for the unified job runner execution paths."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import recording.worker as worker_module
import scheduling.job_runner as job_runner_module
import scheduling.recording_executor as recording_executor_module
import scheduling.upload_runner as upload_runner_module
from database.migrations import run_schema_migrations
from database.models import (
    Base,
    JobStatus,
    Meeting,
    Schedule,
)
from database.models import (
    RecordingJob as RecordingJobModel,
)
from recording.worker import RecordingJob, RecordingResult
from scheduling.job_runner import JobRunner, QueueScheduleResult
from services.runtime_config import RuntimeRecordingConfig
from utils.timezone import utc_now


class FakeWorker:
    """Small worker stub used to drive retry behavior deterministically."""

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
def session_local(tmp_path, monkeypatch):
    """Provide an isolated SQLite session factory for job runner tests."""
    db_path = tmp_path / "job-runner.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(job_runner_module, "get_session_local", lambda: SessionLocal)
    monkeypatch.setattr(recording_executor_module, "get_session_local", lambda: SessionLocal)
    monkeypatch.setattr(recording_executor_module, "notify_recording_completed", AsyncMock())
    monkeypatch.setattr(recording_executor_module, "notify_recording_failed", AsyncMock())
    monkeypatch.setattr(recording_executor_module, "notify_recording_retry", AsyncMock())
    monkeypatch.setattr(recording_executor_module, "notify_recording_status", AsyncMock(return_value=None))
    monkeypatch.setattr(upload_runner_module, "notify_youtube_upload_completed", AsyncMock())
    monkeypatch.setattr(recording_executor_module, "ACTIVE_NOTIFICATION_STATUSES", set())

    class FakeRuntimeConfigService:
        def get_recording_config(
            self,
            session=None,
            *,
            lobby_wait_sec=None,
            resolution_w=None,
            resolution_h=None,
        ):
            return RuntimeRecordingConfig(
                resolution_w=resolution_w if resolution_w is not None else 1600,
                resolution_h=resolution_h if resolution_h is not None else 900,
                lobby_wait_sec=lobby_wait_sec if lobby_wait_sec is not None else 450,
                recordings_dir=tmp_path / "recordings",
                diagnostics_dir=tmp_path / "diagnostics",
                ffmpeg_stall_timeout_sec=120,
                ffmpeg_stall_grace_sec=30,
            )

    monkeypatch.setattr(job_runner_module, "get_runtime_config_service", lambda: FakeRuntimeConfigService())

    settings = SimpleNamespace(
        recordings_dir=tmp_path / "recordings",
        diagnostics_dir=tmp_path / "diagnostics",
        lobby_wait_sec=900,
        resolution_w=1920,
        resolution_h=1080,
    )
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)

    return SessionLocal


class TestJobRunner:
    """Tests for the unified job runner flow."""

    @pytest.mark.asyncio
    async def test_run_immediate_persists_job_and_schedules_direct_execution(self, session_local, monkeypatch):
        """Immediate runs should create the DB row first and then dispatch through the direct-job path."""
        runner = JobRunner()
        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            return Mock()

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        job_id = await runner.run_immediate(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=120,
        )

        assert job_id is not None
        assert len(scheduled) == 1

        scheduled_coro = scheduled[0]
        assert scheduled_coro.cr_code.co_name == "_run_direct_job"
        assert scheduled_coro.cr_frame.f_locals["job"].job_id == job_id
        assert scheduled_coro.cr_frame.f_locals["job"].lobby_wait_sec == 450
        assert scheduled_coro.cr_frame.f_locals["job"].resolution == (1600, 900)
        scheduled_coro.close()

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job_id).first()
            assert db_job is not None
            assert db_job.schedule_id is None
            assert db_job.attempt_no == 1
            assert db_job.retry_count == 0
            assert db_job.status == JobStatus.QUEUED.value
            assert db_job.lobby_wait_sec == 450
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_execute_schedule_uses_same_retry_executor(self, session_local, monkeypatch):
        """Scheduled runs should create a DB row, then delegate to the shared retry executor."""
        runner = JobRunner()
        retry_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(runner, "_run_recording_with_retry", retry_mock)

        session = session_local()
        try:
            meeting = Meeting(
                name="Daily Standup",
                provider="jitsi",
                site_base_url="https://meet.jit.si",
                meeting_code="room-123",
                default_display_name="Recorder Bot",
            )
            schedule = Schedule(
                meeting=meeting,
                schedule_type="once",
                duration_sec=180,
                lobby_wait_sec=777,
                resolution_w=1280,
                resolution_h=720,
                enabled=True,
            )
            session.add(meeting)
            session.add(schedule)
            session.commit()
            schedule_id = schedule.id
        finally:
            session.close()

        await runner._execute_schedule(schedule_id)

        retry_mock.assert_awaited_once()
        kwargs = retry_mock.await_args.kwargs
        assert kwargs["schedule_id"] == schedule_id
        assert kwargs["job"].meeting_code == "room-123"
        assert kwargs["job"].display_name == "Recorder Bot"
        assert kwargs["job"].lobby_wait_sec == 777
        assert kwargs["job"].resolution == (1280, 720)

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.schedule_id == schedule_id).first()
            assert db_job is not None
            assert db_job.job_id == kwargs["job"].job_id
            assert db_job.meeting_code == "room-123"
            assert db_job.status == JobStatus.QUEUED.value
            assert db_job.lobby_wait_sec == 777

            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
            assert schedule.last_started_at is not None
            assert schedule.last_completed_at is not None
            assert schedule.last_run_at == schedule.last_started_at
        finally:
            session.close()

    def test_queue_schedule_returns_queued_when_busy(self, monkeypatch):
        """Busy runner should accept a different schedule into the queue."""
        runner = JobRunner()
        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            return Mock()

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        async def hold_lock():
            await runner._lock.acquire()

        import asyncio

        asyncio.run(hold_lock())
        try:
            result = runner.queue_schedule(1, manual_trigger=True)
            assert result == QueueScheduleResult(
                accepted=True,
                status="queued",
                schedule_id=1,
                queue_position=1,
            )
            assert runner.queue_length == 1
            scheduled[0].close()
        finally:
            runner._lock.release()

    def test_queue_schedule_rejects_duplicate_pending_schedule(self, monkeypatch):
        """A schedule accepted but not yet running should not be accepted twice."""
        runner = JobRunner()
        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            return Mock()

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        first = runner.queue_schedule(1, manual_trigger=True)
        second = runner.queue_schedule(1, manual_trigger=True)

        assert first.accepted is True
        assert first.status == "triggered"
        assert second.accepted is False
        assert second.status == "duplicate"
        scheduled[0].close()

    def test_queue_schedule_treats_pending_processor_as_busy(self, monkeypatch):
        """A second schedule should queue while the first accepted schedule is still pending."""
        runner = JobRunner()
        scheduled = []

        class FakeTask:
            def done(self):
                return False

        def fake_create_task(coro):
            scheduled.append(coro)
            return FakeTask()

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        first = runner.queue_schedule(1, manual_trigger=True)
        second = runner.queue_schedule(2, manual_trigger=True)

        assert first == QueueScheduleResult(
            accepted=True,
            status="triggered",
            schedule_id=1,
            queue_position=0,
        )
        assert second == QueueScheduleResult(
            accepted=True,
            status="queued",
            schedule_id=2,
            queue_position=1,
        )
        assert runner.queue_length == 2
        assert len(scheduled) == 1
        scheduled[0].close()

    @pytest.mark.asyncio
    async def test_manual_schedule_trigger_deadline_uses_trigger_time(self, session_local, monkeypatch):
        """Manual schedule triggers should record for duration_sec even when the planned start is in the future."""
        runner = JobRunner()
        retry_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(runner, "_run_recording_with_retry", retry_mock)

        session = session_local()
        try:
            meeting = Meeting(
                name="Future Manual Trigger",
                provider="zoom",
                meeting_code="https://zoom.us/j/123456789",
                default_display_name="Recorder Bot",
            )
            schedule = Schedule(
                meeting=meeting,
                schedule_type="once",
                start_time=utc_now() + timedelta(days=1),
                duration_sec=180,
                enabled=True,
            )
            session.add(meeting)
            session.add(schedule)
            session.commit()
            schedule_id = schedule.id
        finally:
            session.close()

        before = utc_now()
        await runner._execute_schedule(schedule_id, manual_trigger=True)
        after = utc_now()

        retry_mock.assert_awaited_once()
        deadline = retry_mock.await_args.kwargs["meeting_end_time"]
        assert before + timedelta(seconds=180) <= deadline <= after + timedelta(seconds=180)

    @pytest.mark.asyncio
    async def test_retry_keeps_same_job_id_and_single_db_row(self, session_local, monkeypatch):
        """Retryable failures should reuse one logical job_id and update the same recording_jobs row."""
        runner = JobRunner()

        async def fast_sleep(_seconds):
            return None

        monkeypatch.setattr(recording_executor_module.asyncio, "sleep", fast_sleep)

        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="stable123",
        )

        fail_result = RecordingResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            attempt_no=1,
            error_code="JOIN_FAILED",
            error_message="ERR_NAME_NOT_RESOLVED while joining",
            end_time=utc_now(),
        )
        success_result = RecordingResult(
            job_id=job.job_id,
            status=JobStatus.SUCCEEDED,
            attempt_no=2,
            end_time=utc_now(),
        )
        results = [fail_result, success_result]
        fake_worker = FakeWorker(results)
        monkeypatch.setattr(job_runner_module, "get_worker", lambda: fake_worker)

        session = session_local()
        try:
            runner._persist_job_created(
                session=session,
                job=job,
                schedule_id=None,
                provider=job.provider,
                meeting_code=job.meeting_code,
                display_name=job.display_name,
                base_url=job.base_url,
                duration_sec=job.duration_sec,
                lobby_wait_sec=job.lobby_wait_sec,
            )
            session.commit()
        finally:
            session.close()

        upload_request = await runner._run_recording_with_retry(
            job=job,
            schedule_id=None,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
        )

        assert upload_request is None
        assert fake_worker.calls == [("stable123", 1), ("stable123", 2)]

        session = session_local()
        try:
            rows = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "stable123").all()
            assert len(rows) == 1
            db_job = rows[0]
            assert db_job.status == JobStatus.SUCCEEDED.value
            assert db_job.attempt_no == 2
            assert db_job.retry_count == 1
        finally:
            session.close()
