import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.auth import AuthMiddleware
from api.routes import detection as detection_routes
from api.routes import health, jobs, meetings, schedules, settings, ui, youtube
from api.routes import recording_management as recording_mgmt_routes
from api.routes import telegram as telegram_routes
from config.logging_config import setup_logging
from config.settings import get_settings
from database.models import init_db
from scheduling.job_runner import get_job_runner
from scheduling.scheduler import get_scheduler

# Configure logging with file handler
setup_logging()

settings_config = get_settings()

app = FastAPI(
    title="Meeting Recorder",
    description="Automated online meeting recording system",
    version="0.1.0",
    redirect_slashes=False,  # Don't redirect trailing slashes
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication middleware
app.add_middleware(AuthMiddleware)

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


@app.on_event("startup")
async def startup_event():
    """Application startup tasks."""
    logging.info("Meeting Recorder starting up...")
    logging.info(f"Resolution: {settings_config.resolution_str}")
    logging.info(f"Lobby wait: {settings_config.lobby_wait_sec}s")
    logging.info(f"Jitsi base URL: {settings_config.jitsi_base_url}")

    # Initialize database
    init_db()
    logging.info("Database initialized")

    # Clean up orphaned jobs (jobs stuck in running state from previous session)
    from database.models import JobStatus, RecordingJob, get_session_local

    db = get_session_local()()
    try:
        running_statuses = [
            JobStatus.QUEUED.value,
            JobStatus.STARTING.value,
            JobStatus.JOINING.value,
            JobStatus.WAITING_LOBBY.value,
            JobStatus.RECORDING.value,
            JobStatus.FINALIZING.value,
        ]
        orphaned_jobs = db.query(RecordingJob).filter(RecordingJob.status.in_(running_statuses)).all()
        for job in orphaned_jobs:
            logging.warning(f"Cleaning up orphaned job {job.job_id} (was {job.status})")
            job.status = JobStatus.FAILED.value
            job.error_message = "Job interrupted by server restart"
        if orphaned_jobs:
            db.commit()
            logging.info(f"Cleaned up {len(orphaned_jobs)} orphaned job(s)")
    except Exception as e:
        logging.error(f"Failed to clean up orphaned jobs: {e}")
        db.rollback()
    finally:
        db.close()

    # Start scheduler
    scheduler = get_scheduler()
    job_runner = get_job_runner()

    # Connect scheduler to job runner
    scheduler.set_job_callback(job_runner.queue_schedule)
    scheduler.start()
    logging.info("Scheduler started")

    # Start Telegram bot
    if settings_config.telegram_bot_token:
        from telegram_bot.bot import start_bot

        await start_bot()


@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown tasks."""
    logging.info("Meeting Recorder shutting down...")

    # Stop Telegram bot
    if settings_config.telegram_bot_token:
        from telegram_bot.bot import stop_bot

        await stop_bot()

    # Stop scheduler
    scheduler = get_scheduler()
    scheduler.stop()
    logging.info("Scheduler stopped")
