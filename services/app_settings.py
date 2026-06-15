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
    "lobby_wait_sec": "900",
    "ffmpeg_preset": "ultrafast",
    "ffmpeg_crf": "23",
    "ffmpeg_audio_bitrate": "128k",
    "jitsi_base_url": "https://meet.jit.si/",
    "pre_join_seconds": "30",
    "tz": "Asia/Taipei",
}


def get_setting_defaults(settings: Settings | None = None) -> dict[str, str]:
    """Return editable setting defaults from environment-backed settings."""
    settings = settings or get_settings()
    return {
        "resolution_w": str(settings.resolution_w),
        "resolution_h": str(settings.resolution_h),
        "recording_browser_mode": settings.recording_browser_mode,
        "recording_crop_mode": settings.recording_crop_mode,
        "recording_crop_top_px": str(settings.recording_crop_top_px),
        "lobby_wait_sec": str(settings.lobby_wait_sec),
        "ffmpeg_preset": settings.ffmpeg_preset,
        "ffmpeg_crf": str(settings.ffmpeg_crf),
        "ffmpeg_audio_bitrate": settings.ffmpeg_audio_bitrate,
        "jitsi_base_url": settings.jitsi_base_url,
        "pre_join_seconds": SETTING_DEFAULTS["pre_join_seconds"],
        "tz": settings.tz,
    }


def get_setting(db: Session, key: str) -> str:
    """Get a setting value, falling back to default if not set.

    Args:
        db: Database session
        key: Setting key

    Returns:
        Setting value as string
    """
    setting = db.query(AppSettings).filter(AppSettings.key == key).first()
    if setting:
        return setting.value
    return get_setting_defaults().get(key, "")


def get_setting_int(db: Session, key: str) -> int:
    """Get a setting value as integer."""
    return int(get_setting(db, key))


def set_setting(db: Session, key: str, value: str) -> None:
    """Set a setting value in the database.

    Args:
        db: Database session
        key: Setting key
        value: Setting value (as string)
    """
    setting = db.query(AppSettings).filter(AppSettings.key == key).first()
    if setting:
        setting.value = value
    else:
        setting = AppSettings(key=key, value=value)
        db.add(setting)
    db.commit()


def get_all_settings(db: Session) -> dict[str, str]:
    """Get all settings with defaults.

    Returns:
        Dictionary of all settings with current values
    """
    # Start with environment-backed defaults
    result = get_setting_defaults()

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
    for key, value in settings.items():
        if key in SETTING_DEFAULTS:  # Only allow known settings
            set_setting(db, key, str(value))
