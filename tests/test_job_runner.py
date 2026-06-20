"""Tests for the unified job runner execution paths."""

import asyncio
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
    ErrorCode,
    JobStatus,
    Meeting,
    Schedule,
)
from database.models import (
    RecordingJob as RecordingJobModel,
)
from recording.worker import RecordingJob, RecordingResult
from scheduling.job_runner import JobRunner, QueueScheduleResult, calculate_retry_window_end
from scheduling.recording_executor import RecordingRetryRequest
from scheduling.schedule_queue import QueuedRunItem
from scheduling.upload_runner import UploadRequest
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


def test_calculate_retry_window_end_uses_bounded_dynamic_extension():
    base_end_time = utc_now()
    runtime_config = SimpleNamespace(dynamic_extension_enabled=True, dynamic_extension_max_sec=600)

    assert calculate_retry_window_end(base_end_time, runtime_config) == base_end_time + timedelta(seconds=600)


def test_calculate_retry_window_end_does_not_extend_unbounded_retry_window():
    base_end_time = utc_now()
    runtime_config = SimpleNamespace(dynamic_extension_enabled=True, dynamic_extension_max_sec=0)

    assert calculate_retry_window_end(base_end_time, runtime_config) == base_end_time


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
            smart_trim_enabled=None,
            dynamic_extension_enabled=None,
            dynamic_extension_idle_sec=None,
            dynamic_extension_max_sec=None,
        ):
            return RuntimeRecordingConfig(
                resolution_w=resolution_w if resolution_w is not None else 1600,
                resolution_h=resolution_h if resolution_h is not None else 900,
                lobby_wait_sec=lobby_wait_sec if lobby_wait_sec is not None else 450,
                recordings_dir=tmp_path / "recordings",
                diagnostics_dir=tmp_path / "diagnostics",
                ffmpeg_stall_timeout_sec=120,
                ffmpeg_stall_grace_sec=30,
                recording_browser_mode="app",
                recording_crop_mode="manual",
                recording_crop_top_px=66,
                smart_trim_enabled=True if smart_trim_enabled is None else smart_trim_enabled,
                dynamic_extension_enabled=True if dynamic_extension_enabled is None else dynamic_extension_enabled,
                dynamic_extension_idle_sec=300 if dynamic_extension_idle_sec is None else dynamic_extension_idle_sec,
                dynamic_extension_max_sec=3600 if dynamic_extension_max_sec is None else dynamic_extension_max_sec,
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

        before = utc_now()
        job_id = await runner.run_immediate(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=120,
        )
        after = utc_now()

        assert job_id is not None
        assert len(scheduled) == 1

        scheduled_coro = scheduled[0]
        assert scheduled_coro.cr_code.co_name == "_run_queue_item"
        queue_item = scheduled_coro.cr_frame.f_locals["queue_item"]
        assert queue_item.kind == "immediate"
        assert queue_item.job_id == job_id
        queued_payload = runner._direct_payloads[job_id]
        queued_job = queued_payload.job
        assert queued_job.job_id == job_id
        assert queued_job.lobby_wait_sec == 450
        assert queued_job.resolution == (1600, 900)
        assert queued_job.recording_browser_mode == "app"
        assert queued_job.recording_crop_mode == "manual"
        assert queued_job.recording_crop_top_px == 66
        assert before + timedelta(seconds=3720) <= queued_payload.meeting_end_time <= after + timedelta(seconds=3720)
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
        assert kwargs["job"].recording_browser_mode == "app"
        assert kwargs["job"].recording_crop_mode == "manual"
        assert kwargs["job"].recording_crop_top_px == 66

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
        """Runner at capacity should accept a different schedule into the queue."""
        runner = JobRunner(max_concurrent_recordings=1)
        scheduled = []

        class FakeTask:
            def add_done_callback(self, _callback):
                return None

        def fake_create_task(coro):
            scheduled.append(coro)
            return FakeTask()

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        first = runner.queue_schedule(1, manual_trigger=True)
        second = runner.queue_schedule(2, manual_trigger=True)

        assert first.status == "triggered"
        assert second == QueueScheduleResult(
            accepted=True,
            status="queued",
            schedule_id=2,
            queue_position=1,
        )
        assert runner.queue_length == 1
        scheduled[0].close()

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

    def test_queue_schedule_starts_until_capacity_then_queues(self, monkeypatch):
        """Schedules should start until capacity is full, then queue."""
        runner = JobRunner(max_concurrent_recordings=2)
        scheduled = []

        class FakeTask:
            def add_done_callback(self, _callback):
                return None

        def fake_create_task(coro):
            scheduled.append(coro)
            return FakeTask()

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        first = runner.queue_schedule(1, manual_trigger=True)
        second = runner.queue_schedule(2, manual_trigger=True)
        third = runner.queue_schedule(3, manual_trigger=True)

        assert first == QueueScheduleResult(
            accepted=True,
            status="triggered",
            schedule_id=1,
            queue_position=0,
        )
        assert second == QueueScheduleResult(
            accepted=True,
            status="triggered",
            schedule_id=2,
            queue_position=0,
        )
        assert third == QueueScheduleResult(
            accepted=True,
            status="queued",
            schedule_id=3,
            queue_position=1,
        )
        assert runner.queue_length == 1
        assert len(scheduled) == 2
        for coro in scheduled:
            coro.close()

    @pytest.mark.asyncio
    async def test_fifo_queue_does_not_let_schedule_jump_queued_immediate(self, session_local, monkeypatch):
        """Mixed immediate/schedule work should drain in enqueue order."""
        runner = JobRunner(max_concurrent_recordings=1)
        scheduled = []
        tasks = []

        class FakeTask:
            def add_done_callback(self, _callback):
                return None

            def result(self):
                return None

        def fake_create_task(coro):
            scheduled.append(coro)
            task = FakeTask()
            tasks.append(task)
            return task

        monkeypatch.setattr(job_runner_module.asyncio, "create_task", fake_create_task)

        first = runner.queue_schedule(1, manual_trigger=True)
        job_id = await runner.run_immediate(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=120,
        )
        second_schedule = runner.queue_schedule(2, manual_trigger=True)

        assert first.status == "triggered"
        assert second_schedule.status == "queued"
        assert [item.kind for item in runner.queued_items] == ["immediate", "schedule"]
        assert [item.queue_position for item in runner.queued_items] == [1, 2]

        runner._recording_task_done(tasks[0])

        next_item = scheduled[1].cr_frame.f_locals["queue_item"]
        assert next_item.kind == "immediate"
        assert next_item.job_id == job_id
        for coro in scheduled:
            coro.close()

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
        kwargs = retry_mock.await_args.kwargs
        assert kwargs["job"].duration_sec == 180
        deadline = kwargs["meeting_end_time"]
        assert before + timedelta(seconds=3780) <= deadline <= after + timedelta(seconds=3780)

    @pytest.mark.asyncio
    async def test_legacy_auto_schedule_still_gets_fixed_deadline(self, session_local, monkeypatch):
        """Legacy auto-detect schedules should use duration_sec as the smart-boundary baseline."""
        runner = JobRunner()
        retry_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(runner, "_run_recording_with_retry", retry_mock)

        session = session_local()
        try:
            meeting = Meeting(
                name="Legacy Auto Schedule",
                provider="jitsi",
                meeting_code="legacy-auto-room",
                default_display_name="Recorder Bot",
            )
            schedule = Schedule(
                meeting=meeting,
                schedule_type="once",
                start_time=utc_now() + timedelta(days=1),
                duration_sec=120,
                duration_mode="auto",
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
        kwargs = retry_mock.await_args.kwargs
        assert kwargs["job"].duration_sec == 120
        deadline = kwargs["meeting_end_time"]
        assert before + timedelta(seconds=3720) <= deadline <= after + timedelta(seconds=3720)

    @pytest.mark.asyncio
    async def test_retry_returns_delayed_request_then_keeps_same_job_id(self, session_local, monkeypatch):
        """Retryable failures should release the slot and resume with one logical job row."""
        runner = JobRunner()
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

        retry_request = await runner._run_recording_with_retry(
            job=job,
            schedule_id=None,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
        )

        assert isinstance(retry_request, RecordingRetryRequest)
        assert runner.available_slots == runner.max_concurrent_recordings
        assert fake_worker.calls == [("stable123", 1)]

        session = session_local()
        try:
            rows = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "stable123").all()
            assert len(rows) == 1
            db_job = rows[0]
            assert db_job.status == JobStatus.QUEUED.value
            assert db_job.attempt_no == 1
            assert db_job.retry_count == 0
        finally:
            session.close()

        final_outcome = await runner._run_recording_with_retry(
            job=retry_request.job,
            schedule_id=retry_request.schedule_id,
            meeting_end_time=retry_request.meeting_end_time,
            youtube_enabled=retry_request.youtube_enabled,
            youtube_privacy=retry_request.youtube_privacy,
            meeting_name=retry_request.meeting_name,
            retry_delay_sec=retry_request.next_retry_delay_sec,
        )

        assert final_outcome is None
        assert fake_worker.calls == [("stable123", 1), ("stable123", 2)]

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "stable123").one()
            assert db_job.status == JobStatus.SUCCEEDED.value
            assert db_job.attempt_no == 2
            assert db_job.retry_count == 1
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_delayed_retry_requeues_same_job_without_occupying_slot(self, monkeypatch):
        """Delayed retry wait should not count as an active recording slot."""
        runner = JobRunner(max_concurrent_recordings=1)
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="retryqueue",
        )
        retry_request = RecordingRetryRequest(
            job=job,
            schedule_id=42,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            delay_sec=0,
            next_retry_delay_sec=30,
            error_message="ERR_NAME_NOT_RESOLVED",
        )
        monkeypatch.setattr(runner, "_drain_queues", lambda: None)

        runner._schedule_retry(retry_request)
        assert runner.active_count == 0
        assert runner.available_slots == 1
        assert runner.is_schedule_active_or_queued(42) is True

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert runner.queue_length == 1
        assert runner.queued_items[0].job_id == "retryqueue"
        assert runner.queued_items[0].schedule_id == 42
        assert runner._direct_payloads["retryqueue"].retry_delay_sec == 30

    @pytest.mark.asyncio
    async def test_cancel_delayed_retry_prevents_job_revival(self):
        """Queued retry payloads should be cancelable before they re-enter FIFO."""
        runner = JobRunner(max_concurrent_recordings=1)
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="retrycancel",
        )
        retry_request = RecordingRetryRequest(
            job=job,
            schedule_id=None,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            delay_sec=300,
            next_retry_delay_sec=30,
            error_message="ERR_NAME_NOT_RESOLVED",
        )

        runner._schedule_retry(retry_request)

        assert runner.cancel_queued_job("retrycancel") is True
        await asyncio.sleep(0)

        assert runner.queue_length == 0
        assert "retrycancel" not in runner._retry_requests
        assert "retrycancel" not in runner._direct_payloads

    @pytest.mark.asyncio
    async def test_structured_cancel_reports_retry_waiting_source(self, session_local):
        """Action callers should know whether a queued cancel came from retry waiting."""
        runner = JobRunner(max_concurrent_recordings=1)
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="retry-structured",
        )
        retry_request = RecordingRetryRequest(
            job=job,
            schedule_id=42,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            delay_sec=300,
            next_retry_delay_sec=30,
            error_message="ERR_NAME_NOT_RESOLVED",
        )

        runner._schedule_retry(retry_request)

        result = runner.cancel_queued_job_for_action("retry-structured")
        await asyncio.sleep(0)

        assert result.removed is True
        assert result.source == "retry_waiting"
        assert result.schedule_id == 42
        assert runner.retry_waiting_count == 0

    @pytest.mark.asyncio
    async def test_retry_waiting_items_expose_delayed_retry_without_fifo_position(self, session_local):
        """Delayed retries should be observable before they re-enter the FIFO queue."""
        runner = JobRunner(max_concurrent_recordings=1)
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="retrywait",
        )
        retry_request = RecordingRetryRequest(
            job=job,
            schedule_id=42,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            delay_sec=300,
            next_retry_delay_sec=300,
            error_message="ERR_NAME_NOT_RESOLVED",
        )

        runner._schedule_retry(retry_request)

        waiting_items = runner.retry_waiting_items
        assert runner.queue_length == 0
        assert runner.retry_waiting_count == 1
        assert waiting_items[0].job_id == "retrywait"
        assert waiting_items[0].schedule_id == 42
        assert waiting_items[0].status == "retry_waiting"
        assert waiting_items[0].retry_after_sec > 0

        runner.cancel_queued_job("retrywait")
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_shutdown_marks_interrupted_upload_succeeded(self, session_local, monkeypatch, tmp_path):
        """Shutdown should not leave an upload task stuck in uploading."""
        runner = JobRunner()
        video_path = tmp_path / "recording.mkv"
        video_path.write_bytes(b"video")
        session = session_local()
        try:
            session.add(
                RecordingJobModel(
                    job_id="upload-shutdown",
                    provider="jitsi",
                    meeting_code="room-123",
                    display_name="Recorder Bot",
                    duration_sec=300,
                    status=JobStatus.UPLOADING.value,
                )
            )
            session.commit()
        finally:
            session.close()

        async def blocked_upload(_request):
            await asyncio.sleep(300)

        monkeypatch.setattr(runner, "_run_upload_task", blocked_upload)

        runner._start_upload_task(
            UploadRequest(
                job_id="upload-shutdown",
                video_path=video_path,
                title="Meeting",
                privacy="unlisted",
            )
        )
        await runner.shutdown(upload_timeout_sec=0)

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == "upload-shutdown").one()
            assert db_job.status == JobStatus.SUCCEEDED.value
            assert db_job.error_message == "YouTube upload interrupted by server shutdown"
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_shutdown_clears_pending_retry_state(self):
        """Shutdown should cancel delayed retry tasks and clear process-local retry state."""
        runner = JobRunner()
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="retryshutdown",
        )
        retry_request = RecordingRetryRequest(
            job=job,
            schedule_id=42,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            delay_sec=300,
            next_retry_delay_sec=300,
            error_message="ERR_NAME_NOT_RESOLVED",
        )

        runner._schedule_retry(retry_request)
        await runner.shutdown(upload_timeout_sec=0, recording_timeout_sec=0)

        assert runner.retry_waiting_count == 0
        assert runner.retry_waiting_items == []
        assert runner._retry_tasks == {}

    @pytest.mark.asyncio
    async def test_shutdown_cancels_active_recording_tasks(self):
        """Shutdown should request active recording cancellation and cancel tasks after timeout."""

        class ShutdownWorker:
            def __init__(self):
                self.active_jobs = [SimpleNamespace(job_id="active-shutdown")]
                self.canceled: list[str] = []

            def request_cancel(self, job_id):
                self.canceled.append(job_id)
                return True

        worker = ShutdownWorker()
        runner = JobRunner(worker=worker)
        task = asyncio.create_task(asyncio.sleep(300))
        runner._track_active_task(task)

        await runner.shutdown(upload_timeout_sec=0, recording_timeout_sec=0)

        assert worker.canceled == ["active-shutdown"]
        assert task.cancelled()
        assert runner.active_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_retry_outcome_marks_job_failed_without_new_retry(self, session_local):
        """A retry result produced during shutdown should not create new delayed retry state."""
        runner = JobRunner()
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="retry-no-shutdown",
        )
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

        retry_request = RecordingRetryRequest(
            job=job,
            schedule_id=None,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
            delay_sec=300,
            next_retry_delay_sec=300,
            error_message="ERR_NAME_NOT_RESOLVED",
        )

        runner._shutting_down = True
        runner._handle_recording_outcome(retry_request)

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.error_code == ErrorCode.INTERNAL_ERROR.value
            assert runner.retry_waiting_count == 0
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_direct_queue_item_marks_job_failed_when_payload_missing(self, session_local):
        """A queued DB row should not remain queued if its in-memory payload is gone."""
        runner = JobRunner()
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="lostpayload",
        )

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

        await runner._run_direct_queue_item(QueuedRunItem(kind="immediate", created_at=utc_now(), job_id=job.job_id))

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.error_code == ErrorCode.INTERNAL_ERROR.value
            assert db_job.failure_stage == "dispatch_recording"
            assert db_job.completed_at is not None
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_direct_queue_item_marks_job_failed_on_executor_crash(self, session_local, monkeypatch):
        """Unexpected executor exceptions should leave a terminal DB state."""
        runner = JobRunner()
        job = RecordingJob.create(
            provider="jitsi",
            meeting_code="room-123",
            display_name="Recorder Bot",
            duration_sec=300,
            job_id="crashjob",
        )

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

        runner._direct_payloads[job.job_id] = job_runner_module.DirectRecordingQueueItem(
            job=job,
            meeting_end_time=utc_now() + timedelta(minutes=5),
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
        )
        monkeypatch.setattr(runner, "_run_recording_with_retry", AsyncMock(side_effect=RuntimeError("boom")))

        await runner._run_direct_queue_item(QueuedRunItem(kind="immediate", created_at=utc_now(), job_id=job.job_id))

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.error_code == ErrorCode.INTERNAL_ERROR.value
            assert db_job.failure_stage == "recording_executor"
            assert "boom" in db_job.error_message
        finally:
            session.close()

    @pytest.mark.asyncio
    async def test_schedule_executor_crash_marks_created_job_failed(self, session_local, monkeypatch):
        """A scheduled DB row should not remain queued if executor startup crashes."""
        runner = JobRunner()
        monkeypatch.setattr(runner, "_run_recording_with_retry", AsyncMock(side_effect=RuntimeError("boom")))

        session = session_local()
        try:
            meeting = Meeting(
                name="Crash Schedule",
                provider="jitsi",
                site_base_url="https://meet.jit.si",
                meeting_code="room-123",
                default_display_name="Recorder Bot",
            )
            schedule = Schedule(
                meeting=meeting,
                schedule_type="once",
                duration_sec=180,
                enabled=True,
            )
            session.add(meeting)
            session.add(schedule)
            session.commit()
            schedule_id = schedule.id
        finally:
            session.close()

        await runner._execute_schedule(schedule_id)

        session = session_local()
        try:
            db_job = session.query(RecordingJobModel).filter(RecordingJobModel.schedule_id == schedule_id).one()
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.error_code == ErrorCode.INTERNAL_ERROR.value
            assert db_job.failure_stage == "recording_executor"
            assert "boom" in db_job.error_message
        finally:
            session.close()
