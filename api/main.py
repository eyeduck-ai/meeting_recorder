import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.auth import AuthMiddleware
from api.cors import configure_cors
from api.routes import detection as detection_routes
from api.routes import health, jobs, meetings, schedules, settings, ui, youtube
from api.routes import recording_management as recording_mgmt_routes
from api.routes import telegram as telegram_routes
from config.logging_config import setup_logging
from config.settings import get_settings
from database.models import JobStatus, RecordingJob
from database.session import get_session_local, init_db
from recording.worker import RecordingWorker, reset_worker_instance, set_worker_instance
from scheduling.job_runner import JobRunner, reset_job_runner_instance, set_job_runner_instance
from scheduling.scheduler import SchedulerService, reset_scheduler_instance, set_scheduler_instance
from uploading.youtube import close_youtube_uploader
from utils.timezone import utc_now

# Configure logging with file handler
setup_logging()

settings_config = get_settings()


def _job_has_existing_recording_file(job: RecordingJob) -> bool:
    for path_value in (job.raw_output_path, job.output_path):
        if not path_value:
            continue
        try:
            if Path(path_value).exists():
                return True
        except OSError:
            continue
    return False


def cleanup_orphaned_jobs() -> None:
    """Mark jobs left in active states as failed after process restart."""
    db = get_session_local()()
    try:
        running_statuses = [
            JobStatus.QUEUED.value,
            JobStatus.STARTING.value,
            JobStatus.JOINING.value,
            JobStatus.WAITING_LOBBY.value,
            JobStatus.RECORDING.value,
        ]
        orphaned_jobs = db.query(RecordingJob).filter(RecordingJob.status.in_(running_statuses)).all()
        stale_finalizing = db.query(RecordingJob).filter(RecordingJob.status == JobStatus.FINALIZING.value).all()
        restored_finalizing_count = 0
        for job in stale_finalizing:
            if _job_has_existing_recording_file(job):
                logging.warning(f"Restoring interrupted post-processing job {job.job_id}")
                job.status = JobStatus.SUCCEEDED.value
                job.error_message = "Recording post-processing interrupted by server restart"
                job.completed_at = utc_now()
                if not job.end_reason:
                    job.end_reason = "completed"
                restored_finalizing_count += 1
            else:
                orphaned_jobs.append(job)
        for job in orphaned_jobs:
            logging.warning(f"Cleaning up orphaned job {job.job_id} (was {job.status})")
            job.status = JobStatus.FAILED.value
            job.error_message = "Job interrupted by server restart"
        stale_uploads = db.query(RecordingJob).filter(RecordingJob.status == JobStatus.UPLOADING.value).all()
        for job in stale_uploads:
            logging.warning(f"Restoring interrupted upload job {job.job_id}")
            job.status = JobStatus.SUCCEEDED.value
            job.error_message = "YouTube upload interrupted by server restart"
        if orphaned_jobs or stale_uploads or restored_finalizing_count:
            db.commit()
            logging.info(
                "Cleaned up %s orphaned job(s), restored %s interrupted finalizing job(s), "
                "restored %s interrupted upload job(s)",
                len(orphaned_jobs),
                restored_finalizing_count,
                len(stale_uploads),
            )
    except Exception as e:
        logging.error(f"Failed to clean up orphaned jobs: {e}")
        db.rollback()
    finally:
        db.close()


def _clear_runtime_state(app: FastAPI) -> None:
    for attr in ("worker", "job_runner", "scheduler"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own application runtime dependencies for the FastAPI process."""
    logging.info("Meeting Recorder starting up...")
    logging.info(f"Resolution: {settings_config.resolution_str}")
    logging.info(f"Lobby wait: {settings_config.lobby_wait_sec}s")
    logging.info(f"Jitsi base URL: {settings_config.jitsi_base_url}")

    scheduler = None
    job_runner = None
    telegram_started = False
    try:
        init_db()
        logging.info("Database initialized")

        cleanup_orphaned_jobs()

        worker = RecordingWorker()
        job_runner = JobRunner(worker=worker)
        scheduler = SchedulerService()

        app.state.worker = worker
        app.state.job_runner = job_runner
        app.state.scheduler = scheduler

        set_worker_instance(worker)
        set_job_runner_instance(job_runner)
        set_scheduler_instance(scheduler)

        scheduler.set_job_callback(job_runner.queue_schedule)
        scheduler.start()
        logging.info("Scheduler started")

        if settings_config.telegram_bot_token:
            from telegram_bot.bot import start_bot

            await start_bot()
            telegram_started = True

        yield
    finally:
        logging.info("Meeting Recorder shutting down...")

        if telegram_started:
            from telegram_bot.bot import stop_bot

            await stop_bot()

        if scheduler is not None:
            scheduler.stop()
            logging.info("Scheduler stopped")

        if job_runner is not None:
            await job_runner.shutdown()

        await close_youtube_uploader()

        _clear_runtime_state(app)
        reset_scheduler_instance()
        reset_job_runner_instance()
        reset_worker_instance()


app = FastAPI(
    title="Meeting Recorder",
    description="Automated online meeting recording system",
    version="0.1.0",
    redirect_slashes=False,  # Don't redirect trailing slashes
    lifespan=lifespan,
)

# Authentication middleware
app.add_middleware(AuthMiddleware)

# CORS is added after auth so Starlette wraps it outside auth when enabled.
configure_cors(app, settings_config.cors_allowed_origin_list)

# Include routers
app.include_router(health.router)
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(meetings.router, prefix="/api/v1")
app.include_router(schedules.router, prefix="/api/v1")
app.include_router(youtube.router, prefix="/api/v1")
app.include_router(telegram_routes.router, prefix="/api/v1")
app.include_router(detection_routes.router)  # Detection API
app.include_router(recording_mgmt_routes.router)  # Recording management API
app.include_router(settings.router)

# Web UI routes (must be last to avoid catching API routes)
app.include_router(ui.router)

# Mount static files
static_dir = Path(__file__).parent.parent / "web" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
