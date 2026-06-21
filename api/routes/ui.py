"""Aggregate Web UI router.

Route implementations live in focused child modules. Keep this module as the
single include point for FastAPI.
"""

from fastapi import APIRouter

from api.routes import (
    ui_auth,
    ui_dashboard,
    ui_jobs,
    ui_meetings,
    ui_recordings,
    ui_schedules,
    ui_settings,
)

router = APIRouter(tags=["ui"])

router.include_router(ui_auth.router)
router.include_router(ui_dashboard.router)
router.include_router(ui_meetings.router)
router.include_router(ui_schedules.router)
router.include_router(ui_settings.router)
router.include_router(ui_jobs.router)
router.include_router(ui_recordings.router)
