"""Service layer for managing application settings stored in the database."""

from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from database.models import AppSettings

# Default values for all configurable settings
SETTING_DEFAULTS = {
    "resolution_w": "1920",
    "resolution_h": "1080",
    "recording_browser_mode": "app",
    "recording_crop_mode": "off",
    "recording_crop_top_px": "0",
    "smart_trim_enabled": "true",
    "dynamic_extension_enabled": "true",
    "dynamic_extension_idle_sec": "300",
    "dynamic_extension_max_sec": "3600",
    "activity_audio_threshold_db": "-45.0",
    "activity_video_diff_threshold": "0.015",
    "activity_sample_interval_sec": "5.0",
    "activity_sample_window_sec": "1.0",
    "smart_trim_pre_roll_sec": "2.0",
    "smart_trim_end_post_roll_sec": "5.0",
    "lobby_wait_sec": "900",
    "ffmpeg_preset": "ultrafast",
    "ffmpeg_crf": "23",
    "ffmpeg_audio_bitrate": "128k",
    "jitsi_base_url": "https://meet.jit.si/",
    "pre_join_seconds": "30",
    "tz": "Asia/Taipei",
}


def _get_setting_defaults(settings: Settings | None = None) -> dict[str, str]:
    """Return editable setting defaults from environment-backed settings."""
    settings = settings or get_settings()
    return {
        "resolution_w": str(settings.resolution_w),
        "resolution_h": str(settings.resolution_h),
        "recording_browser_mode": settings.recording_browser_mode,
        "recording_crop_mode": settings.recording_crop_mode,
        "recording_crop_top_px": str(settings.recording_crop_top_px),
        "smart_trim_enabled": str(settings.smart_trim_enabled).lower(),
        "dynamic_extension_enabled": str(settings.dynamic_extension_enabled).lower(),
        "dynamic_extension_idle_sec": str(settings.dynamic_extension_idle_sec),
        "dynamic_extension_max_sec": str(settings.dynamic_extension_max_sec),
        "activity_audio_threshold_db": str(settings.activity_audio_threshold_db),
        "activity_video_diff_threshold": str(settings.activity_video_diff_threshold),
        "activity_sample_interval_sec": str(settings.activity_sample_interval_sec),
        "activity_sample_window_sec": str(settings.activity_sample_window_sec),
        "smart_trim_pre_roll_sec": str(settings.smart_trim_pre_roll_sec),
        "smart_trim_end_post_roll_sec": str(settings.smart_trim_end_post_roll_sec),
        "lobby_wait_sec": str(settings.lobby_wait_sec),
        "ffmpeg_preset": settings.ffmpeg_preset,
        "ffmpeg_crf": str(settings.ffmpeg_crf),
        "ffmpeg_audio_bitrate": settings.ffmpeg_audio_bitrate,
        "jitsi_base_url": settings.jitsi_base_url,
        "pre_join_seconds": SETTING_DEFAULTS["pre_join_seconds"],
        "tz": settings.tz,
    }


def get_all_settings(db: Session) -> dict[str, str]:
    """Get all settings with defaults.

    Returns:
        Dictionary of all settings with current values
    """
    # Start with environment-backed defaults
    result = _get_setting_defaults()

    # Override with database values
    settings = db.query(AppSettings).all()
    for setting in settings:
        result[setting.key] = setting.value

    return result


def update_settings(db: Session, settings: dict[str, str]) -> None:
    """Update multiple settings at once.

    Args:
        db: Database session
        settings: Dictionary of key-value pairs to update
    """
    allowed_updates = {key: str(value) for key, value in settings.items() if key in SETTING_DEFAULTS}
    if not allowed_updates:
        return

    existing = db.query(AppSettings).filter(AppSettings.key.in_(allowed_updates)).all()
    by_key = {setting.key: setting for setting in existing}

    for key, value in allowed_updates.items():
        setting = by_key.get(key)
        if setting:
            setting.value = value
        else:
            db.add(AppSettings(key=key, value=value))

    db.commit()
