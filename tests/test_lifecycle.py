import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, JobStatus, RecordingJob


def test_jobs_route_import_does_not_initialize_database(monkeypatch):
    """Importing the jobs route must not run schema initialization."""
    import database.session as session_module

    called = False

    def fake_init_db():
        nonlocal called
        called = True

    monkeypatch.setattr(session_module, "init_db", fake_init_db)
    sys.modules.pop("api.routes.jobs", None)
    importlib.import_module("api.routes.jobs")

    assert called is False


def test_services_package_import_does_not_import_service_modules():
    """Importing the services package should not eagerly import concrete services."""
    sys.modules.pop("services", None)
    for module_name in (
        "services.notification",
        "services.recording_manager",
        "services.schedule_service",
        "services.job_service",
    ):
        sys.modules.pop(module_name, None)

    importlib.import_module("services")

    for module_name in (
        "services.notification",
        "services.recording_manager",
        "services.schedule_service",
        "services.job_service",
    ):
        assert module_name not in sys.modules


def test_runtime_state_helpers_are_not_reexported_from_routes():
    """Runtime-state payload helpers should stay in the service owner module."""
    sys.modules.pop("api.routes.job_queue_payloads", None)

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("api.routes.job_queue_payloads")


def test_telegram_package_import_does_not_expose_db_session_helper():
    """Telegram package import should not eagerly load DB session helpers."""
    original_telegram_bot = sys.modules.pop("telegram_bot", None)
    original_database_session = sys.modules.pop("database.session", None)
    try:
        package = importlib.import_module("telegram_bot")

        assert not hasattr(package, "get_db_session")
        assert "database.session" not in sys.modules
    finally:
        if original_telegram_bot is not None:
            sys.modules["telegram_bot"] = original_telegram_bot
        else:
            sys.modules.pop("telegram_bot", None)

        if original_database_session is not None:
            sys.modules["database.session"] = original_database_session
        else:
            sys.modules.pop("database.session", None)


def test_notification_service_does_not_keep_unwired_event_methods():
    import services.notification as notification_module
    from services.notification import NotificationConfig, NotificationService

    service = NotificationService()

    assert not hasattr(notification_module, "EmailNotifier")
    assert not hasattr(notification_module, "WebhookNotifier")

    for method_name in (
        "notify_recording_started",
        "notify_recording_completed",
        "notify_recording_failed",
        "notify_disk_space_low",
    ):
        assert not hasattr(service, method_name)

    notification_module._notification_service = NotificationService(
        NotificationConfig(smtp_enabled=True, smtp_host="smtp.example.com")
    )
    notification_module.reset_notification_service()

    assert notification_module._notification_service is None


@pytest.mark.asyncio
async def test_lifespan_owns_runtime_instances(monkeypatch):
    """FastAPI lifespan should create runtime objects and expose them through app.state."""
    import api.main as main_module
    import recording.worker as worker_module
    import scheduling.job_runner as job_runner_module
    import scheduling.scheduler as scheduler_module

    calls = []
    scheduler_instances = []

    class FakeWorker:
        pass

    class FakeJobRunner:
        def __init__(self, *, worker):
            self.worker = worker
            self.queue_schedule = lambda *_args, **_kwargs: None
            self.shutdown_called = False

        async def shutdown(self):
            self.shutdown_called = True
            calls.append("runner_shutdown")

    class FakeScheduler:
        is_running = False

        def __init__(self):
            self.callback = None
            self.started = False
            self.stopped = False
            scheduler_instances.append(self)

        def set_job_callback(self, callback):
            self.callback = callback

        def start(self):
            self.started = True
            self.is_running = True

        def stop(self):
            self.stopped = True
            self.is_running = False

    monkeypatch.setattr(
        main_module,
        "settings_config",
        SimpleNamespace(
            resolution_str="1x1",
            lobby_wait_sec=1,
            jitsi_base_url="https://meet.jit.si/",
            telegram_bot_token="",
        ),
    )
    monkeypatch.setattr(main_module, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(main_module, "cleanup_orphaned_jobs", lambda: calls.append("cleanup"))
    monkeypatch.setattr(main_module, "RecordingWorker", FakeWorker)
    monkeypatch.setattr(main_module, "JobRunner", FakeJobRunner)
    monkeypatch.setattr(main_module, "SchedulerService", FakeScheduler)

    async def fake_close_youtube_uploader():
        calls.append("close_youtube")

    monkeypatch.setattr(main_module, "close_youtube_uploader", fake_close_youtube_uploader)

    app = FastAPI()
    async with main_module.lifespan(app):
        assert calls == ["init_db", "cleanup"]
        assert isinstance(app.state.worker, FakeWorker)
        assert app.state.job_runner.worker is app.state.worker
        assert app.state.scheduler.callback is app.state.job_runner.queue_schedule
        assert app.state.scheduler.started is True
        assert worker_module.get_worker() is app.state.worker
        assert job_runner_module.get_job_runner() is app.state.job_runner
        assert scheduler_module.get_scheduler() is app.state.scheduler

    assert scheduler_instances[0].stopped is True
    assert calls[-2:] == ["runner_shutdown", "close_youtube"]
    assert not hasattr(app.state, "worker")
    assert not hasattr(app.state, "job_runner")
    assert not hasattr(app.state, "scheduler")
    assert not hasattr(main_module, "_clear_runtime_state")


@pytest.mark.asyncio
async def test_start_recording_route_uses_app_state_job_runner(tmp_path):
    """The jobs route should bind JobService to the app-state job runner."""
    from api.routes.jobs import RecordRequest, start_recording

    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle-jobs.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()

    class FakeRunner:
        async def run_immediate(self, **_kwargs):
            session.add(
                RecordingJob(
                    job_id="route-state-job",
                    provider="jitsi",
                    meeting_code="room",
                    display_name="Recorder Bot",
                    duration_sec=3600,
                    status=JobStatus.QUEUED.value,
                )
            )
            session.commit()
            return "route-state-job"

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(job_runner=FakeRunner())))
    try:
        response = await start_recording(
            RecordRequest(
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder Bot",
                duration_sec=3600,
            ),
            http_request=request,
            db=session,
        )
    finally:
        session.close()

    assert response.job_id == "route-state-job"


def test_cleanup_orphaned_jobs_restores_stale_uploading(monkeypatch, tmp_path):
    """Startup cleanup should not leave interrupted uploads stuck in uploading."""
    import api.main as main_module

    engine = create_engine(f"sqlite:///{tmp_path / 'cleanup.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        session.add(
            RecordingJob(
                job_id="upload-stale",
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder Bot",
                duration_sec=3600,
                status=JobStatus.UPLOADING.value,
                output_path=str(tmp_path / "recording.mkv"),
            )
        )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(main_module, "get_session_local", lambda: SessionLocal)

    main_module.cleanup_orphaned_jobs()

    session = SessionLocal()
    try:
        job = session.query(RecordingJob).filter(RecordingJob.job_id == "upload-stale").one()
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.output_path == str(tmp_path / "recording.mkv")
        assert job.error_message == "YouTube upload interrupted by server restart"
    finally:
        session.close()


def test_cleanup_orphaned_jobs_restores_stale_finalizing_with_existing_recording(monkeypatch, tmp_path):
    """Interrupted post-processing should preserve successful raw recordings after restart."""
    import api.main as main_module

    raw_path = tmp_path / "recording.mkv"
    raw_path.write_bytes(b"raw")
    engine = create_engine(f"sqlite:///{tmp_path / 'cleanup-finalizing.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        session.add(
            RecordingJob(
                job_id="finalizing-stale",
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder Bot",
                duration_sec=3600,
                status=JobStatus.FINALIZING.value,
                raw_output_path=str(raw_path),
                output_path=str(raw_path),
            )
        )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(main_module, "get_session_local", lambda: SessionLocal)

    main_module.cleanup_orphaned_jobs()

    session = SessionLocal()
    try:
        job = session.query(RecordingJob).filter(RecordingJob.job_id == "finalizing-stale").one()
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.error_message == "Recording post-processing interrupted by server restart"
        assert job.completed_at is not None
    finally:
        session.close()


def test_cleanup_orphaned_jobs_fails_stale_finalizing_without_recording(monkeypatch, tmp_path):
    """Finalizing jobs without any persisted recording file remain failed after restart cleanup."""
    import api.main as main_module

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cleanup-finalizing-missing.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        session.add(
            RecordingJob(
                job_id="finalizing-missing",
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder Bot",
                duration_sec=3600,
                status=JobStatus.FINALIZING.value,
                raw_output_path=str(tmp_path / "missing.mkv"),
            )
        )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(main_module, "get_session_local", lambda: SessionLocal)

    main_module.cleanup_orphaned_jobs()

    session = SessionLocal()
    try:
        job = session.query(RecordingJob).filter(RecordingJob.job_id == "finalizing-missing").one()
        assert job.status == JobStatus.FAILED.value
        assert job.error_message == "Job interrupted by server restart"
    finally:
        session.close()
