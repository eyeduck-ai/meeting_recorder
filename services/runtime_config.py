"""Resolve effective runtime recording configuration."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from database.models import AppSettings

RUNTIME_DB_KEYS = {
    "resolution_w",
    "resolution_h",
    "lobby_wait_sec",
}


class RuntimeConfigError(ValueError):
    """Raised when persisted runtime settings cannot be parsed."""


@dataclass(frozen=True)
class RuntimeRecordingConfig:
    """Effective configuration for one recording runtime."""

    resolution_w: int
    resolution_h: int
    lobby_wait_sec: int
    recordings_dir: Path
    diagnostics_dir: Path
    ffmpeg_stall_timeout_sec: int
    ffmpeg_stall_grace_sec: int

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.resolution_w, self.resolution_h)

    @property
    def resolution_str(self) -> str:
        return f"{self.resolution_w}x{self.resolution_h}"


class RuntimeConfigService:
    """Resolve DB/app overrides over environment-backed settings."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def get_recording_config(
        self,
        db: Session | None = None,
        *,
        lobby_wait_sec: int | None = None,
        resolution_w: int | None = None,
        resolution_h: int | None = None,
    ) -> RuntimeRecordingConfig:
        """Return effective config using override > DB > env precedence."""
        values = {
            "resolution_w": self._setting_value("resolution_w", 1920),
            "resolution_h": self._setting_value("resolution_h", 1080),
            "lobby_wait_sec": self._setting_value("lobby_wait_sec", 900),
        }

        if db is not None:
            values.update(self._load_db_values(db))

        overrides = {
            "resolution_w": resolution_w,
            "resolution_h": resolution_h,
            "lobby_wait_sec": lobby_wait_sec,
        }
        for key, value in overrides.items():
            if value is not None:
                values[key] = value

        resolved_resolution_w = self._parse_positive_int("resolution_w", values["resolution_w"])
        resolved_resolution_h = self._parse_positive_int("resolution_h", values["resolution_h"])
        resolved_lobby_wait_sec = self._parse_int_range(
            "lobby_wait_sec",
            values["lobby_wait_sec"],
            minimum=0,
            maximum=1800,
        )

        return RuntimeRecordingConfig(
            resolution_w=resolved_resolution_w,
            resolution_h=resolved_resolution_h,
            lobby_wait_sec=resolved_lobby_wait_sec,
            recordings_dir=Path(self._setting_value("recordings_dir", Path("./recordings"))),
            diagnostics_dir=Path(self._setting_value("diagnostics_dir", Path("./diagnostics"))),
            ffmpeg_stall_timeout_sec=self._setting_value("ffmpeg_stall_timeout_sec", 120),
            ffmpeg_stall_grace_sec=self._setting_value("ffmpeg_stall_grace_sec", 30),
        )

    def _load_db_values(self, db: Session) -> dict[str, str]:
        records = db.query(AppSettings).filter(AppSettings.key.in_(RUNTIME_DB_KEYS)).all()
        return {record.key: record.value for record in records}

    def _setting_value(self, key: str, default: object) -> object:
        model_fields = getattr(type(self.settings), "model_fields", None)
        if isinstance(model_fields, dict) and key in model_fields:
            return getattr(self.settings, key)

        settings_vars = vars(self.settings)
        if key in settings_vars:
            return settings_vars[key]

        return default

    def _parse_positive_int(self, key: str, value: object) -> int:
        parsed = self._parse_int(key, value)
        if parsed <= 0:
            raise RuntimeConfigError(f"{key} must be a positive integer")
        return parsed

    def _parse_int_range(self, key: str, value: object, *, minimum: int, maximum: int) -> int:
        parsed = self._parse_int(key, value)
        if parsed < minimum or parsed > maximum:
            raise RuntimeConfigError(f"{key} must be between {minimum} and {maximum}")
        return parsed

    def _parse_int(self, key: str, value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError(f"{key} must be an integer") from exc


def get_runtime_config_service(settings: Settings | None = None) -> RuntimeConfigService:
    """Create a runtime config resolver."""
    return RuntimeConfigService(settings=settings)
