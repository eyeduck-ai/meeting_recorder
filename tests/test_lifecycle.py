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
    import database.models as models

    called = False

    def fake_init_db():
        nonlocal called
        called = True

    monkeypatch.setattr(models, "init_db", fake_init_db)
    sys.modules.pop("api.routes.jobs", None)
    importlib.import_module("api.routes.jobs")

    assert called is False


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
    assert calls[-1] == "close_youtube"
    assert not hasattr(app.state, "worker")
    assert not hasattr(app.state, "job_runner")
    assert not hasattr(app.state, "scheduler")


@pytest.mark.asyncio
async def test_start_recording_route_uses_app_state_job_runner(tmp_path):
    """The jobs route should bind JobService to the app-state job runner."""
    from api.routes.jobs import RecordRequest, start_recording

    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle-jobs.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()

    class FakeRunner:
        is_busy = False

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
