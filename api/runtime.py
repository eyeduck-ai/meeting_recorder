"""FastAPI app-state runtime dependency helpers."""

from fastapi import Request

from recording.worker import get_worker
from scheduling.job_runner import get_job_runner
from scheduling.scheduler import get_scheduler
from services.job_service import JobService
from services.schedule_service import ScheduleService


def get_app_worker(request: Request):
    """Return the app-owned worker, falling back to the compatibility singleton."""
    return getattr(request.app.state, "worker", None) or get_worker()


def get_app_job_runner(request: Request):
    """Return the app-owned job runner, falling back to the compatibility singleton."""
    return getattr(request.app.state, "job_runner", None) or get_job_runner()


def get_app_scheduler(request: Request):
    """Return the app-owned scheduler, falling back to the compatibility singleton."""
    return getattr(request.app.state, "scheduler", None) or get_scheduler()


def get_app_job_service(request: Request) -> JobService:
    """Return a job service bound to the app-owned job runner."""
    return JobService(job_runner=get_app_job_runner(request))


def get_app_schedule_service(request: Request) -> ScheduleService:
    """Return a schedule service bound to app-owned runtime objects."""
    return ScheduleService(
        scheduler=get_app_scheduler(request),
        job_runner=get_app_job_runner(request),
    )
