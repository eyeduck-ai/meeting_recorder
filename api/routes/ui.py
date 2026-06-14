"""Aggregate Web UI router.

Route implementations live in focused child modules. Keep this module as the
single include point for FastAPI and as a short-term compatibility surface for
older tests/imports.
"""

from fastapi import APIRouter

from api import runtime as api_runtime
from api.routes import (
    ui_auth,
    ui_common,
    ui_dashboard,
    ui_job_diagnostics,
    ui_jobs,
    ui_meetings,
    ui_recordings,
    ui_schedules,
    ui_settings,
)
from database.session import get_db as get_db

router = APIRouter(tags=["ui"])

# Compatibility re-exports. New code should import from the owning module.
settings = ui_common.settings
templates = ui_common.templates
localtime_filter = ui_common.localtime_filter
get_context = ui_common.get_context
render_template = ui_common.render_template

get_app_schedule_service = api_runtime.get_app_schedule_service
get_app_worker = api_runtime.get_app_worker

JOB_LOG_EXCERPT_BYTES = ui_job_diagnostics.JOB_LOG_EXCERPT_BYTES
FailureContextView = ui_job_diagnostics.FailureContextView
JobLogView = ui_job_diagnostics.JobLogView
_load_failure_context = ui_job_diagnostics._load_failure_context
_load_job_logs = ui_job_diagnostics._load_job_logs
_read_text_excerpt = ui_job_diagnostics._read_text_excerpt
_resolve_job_log_path = ui_job_diagnostics._resolve_job_log_path

router.include_router(ui_auth.router)
router.include_router(ui_dashboard.router)
router.include_router(ui_meetings.router)
router.include_router(ui_schedules.router)
router.include_router(ui_settings.router)
router.include_router(ui_jobs.router)
router.include_router(ui_recordings.router)
