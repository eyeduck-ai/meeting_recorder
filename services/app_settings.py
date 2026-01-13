"""Service layer for managing application settings stored in the database."""

from sqlalchemy.orm import Session

from database.models import AppSettings

# Default values for all configurable settings
SETTING_DEFAULTS = {
    "resolution_w": "1920",
    "resolution_h": "1080",
    "lobby_wait_sec": "900",
    "ffmpeg_preset": "ultrafast",
    "ffmpeg_crf": "23",
    "ffmpeg_audio_bitrate": "128k",
    "jitsi_base_url": "https://meet.jit.si/",
    "pre_join_seconds": "30",
    "tz": "Asia/Taipei",
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
    return SETTING_DEFAULTS.get(key, "")


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
    # Start with defaults
    result = dict(SETTING_DEFAULTS)

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
