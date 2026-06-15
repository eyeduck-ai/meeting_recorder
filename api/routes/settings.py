"""API routes for application settings management."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.session import get_db
from services.app_settings import get_all_settings, update_settings

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    """Request model for updating settings."""

    resolution_w: int | None = None
    resolution_h: int | None = None
    recording_browser_mode: Literal["app", "normal"] | None = None
    recording_crop_mode: Literal["auto", "manual", "off"] | None = None
    recording_crop_top_px: int | None = Field(default=None, ge=0)
    smart_trim_enabled: bool | None = None
    dynamic_extension_enabled: bool | None = None
    dynamic_extension_idle_sec: int | None = Field(default=None, ge=1, le=14400)
    dynamic_extension_max_sec: int | None = Field(default=None, ge=0, le=86400)
    activity_audio_threshold_db: float | None = None
    activity_video_diff_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    activity_sample_interval_sec: float | None = Field(default=None, ge=1.0, le=300.0)
    activity_sample_window_sec: float | None = Field(default=None, ge=0.25, le=30.0)
    smart_trim_pre_roll_sec: float | None = Field(default=None, ge=0.0, le=60.0)
    smart_trim_end_post_roll_sec: float | None = Field(default=None, ge=0.0, le=300.0)
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
        "recording_browser_mode": settings["recording_browser_mode"],
        "recording_crop_mode": settings["recording_crop_mode"],
        "recording_crop_top_px": int(settings["recording_crop_top_px"]),
        "smart_trim_enabled": settings["smart_trim_enabled"].lower() == "true",
        "dynamic_extension_enabled": settings["dynamic_extension_enabled"].lower() == "true",
        "dynamic_extension_idle_sec": int(settings["dynamic_extension_idle_sec"]),
        "dynamic_extension_max_sec": int(settings["dynamic_extension_max_sec"]),
        "activity_audio_threshold_db": float(settings["activity_audio_threshold_db"]),
        "activity_video_diff_threshold": float(settings["activity_video_diff_threshold"]),
        "activity_sample_interval_sec": float(settings["activity_sample_interval_sec"]),
        "activity_sample_window_sec": float(settings["activity_sample_window_sec"]),
        "smart_trim_pre_roll_sec": float(settings["smart_trim_pre_roll_sec"]),
        "smart_trim_end_post_roll_sec": float(settings["smart_trim_end_post_roll_sec"]),
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

    current = get_all_settings(db)
    effective_resolution_h = int(updates.get("resolution_h", current["resolution_h"]))
    effective_crop_top = int(updates.get("recording_crop_top_px", current["recording_crop_top_px"]))
    if effective_crop_top >= effective_resolution_h:
        raise HTTPException(
            status_code=422,
            detail="recording_crop_top_px must be smaller than resolution_h",
        )
    effective_extension_idle = int(updates.get("dynamic_extension_idle_sec", current["dynamic_extension_idle_sec"]))
    effective_extension_max = int(updates.get("dynamic_extension_max_sec", current["dynamic_extension_max_sec"]))
    if effective_extension_max and effective_extension_max < effective_extension_idle:
        raise HTTPException(
            status_code=422,
            detail="dynamic_extension_max_sec must be 0 or greater than dynamic_extension_idle_sec",
        )

    update_settings(db, updates)

    # Return updated settings
    return get_settings_endpoint(db)
