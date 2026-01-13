"""API routes for application settings management."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.models import get_db
from services.app_settings import get_all_settings, update_settings

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    """Request model for updating settings."""

    resolution_w: int | None = None
    resolution_h: int | None = None
    lobby_wait_sec: int | None = None
    ffmpeg_preset: str | None = None
    ffmpeg_crf: int | None = None
    ffmpeg_audio_bitrate: str | None = None
    jitsi_base_url: str | None = None
    pre_join_seconds: int | None = None
    tz: str | None = None


@router.get("")
def get_settings_endpoint(db: Session = Depends(get_db)):
    """Get all editable settings."""
    settings = get_all_settings(db)
    # Convert string values to appropriate types for response
    return {
        "resolution_w": int(settings["resolution_w"]),
        "resolution_h": int(settings["resolution_h"]),
        "lobby_wait_sec": int(settings["lobby_wait_sec"]),
        "ffmpeg_preset": settings["ffmpeg_preset"],
        "ffmpeg_crf": int(settings["ffmpeg_crf"]),
        "ffmpeg_audio_bitrate": settings["ffmpeg_audio_bitrate"],
        "jitsi_base_url": settings["jitsi_base_url"],
        "pre_join_seconds": int(settings["pre_join_seconds"]),
        "tz": settings["tz"],
    }


@router.put("")
def update_settings_endpoint(settings: SettingsUpdate, db: Session = Depends(get_db)):
    """Update settings."""
    # Convert to dict, filtering out None values
    updates = {}
    for key, value in settings.model_dump().items():
        if value is not None:
            updates[key] = str(value)

    update_settings(db, updates)

    # Return updated settings
    return get_settings_endpoint(db)
