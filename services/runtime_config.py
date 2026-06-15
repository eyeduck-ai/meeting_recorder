"""Resolve effective runtime recording configuration."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from database.models import AppSettings
from recording.activity import ActivityConfig, build_activity_config

RUNTIME_DB_KEYS = {
    "resolution_w",
    "resolution_h",
    "recording_browser_mode",
    "recording_crop_mode",
    "recording_crop_top_px",
    "smart_trim_enabled",
    "dynamic_extension_enabled",
    "dynamic_extension_idle_sec",
    "dynamic_extension_max_sec",
    "activity_audio_threshold_db",
    "activity_video_diff_threshold",
    "activity_sample_interval_sec",
    "activity_sample_window_sec",
    "smart_trim_pre_roll_sec",
    "smart_trim_end_post_roll_sec",
    "lobby_wait_sec",
}

VALID_RECORDING_BROWSER_MODES = {"app", "normal"}
VALID_RECORDING_CROP_MODES = {"auto", "manual", "off"}


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
    recording_browser_mode: str = "app"
    recording_crop_mode: str = "off"
    recording_crop_top_px: int = 0
    smart_trim_enabled: bool = True
    dynamic_extension_enabled: bool = True
    dynamic_extension_idle_sec: int = 300
    dynamic_extension_max_sec: int = 3600
    activity_audio_threshold_db: float = -45.0
    activity_video_diff_threshold: float = 0.015
    activity_sample_interval_sec: float = 5.0
    activity_sample_window_sec: float = 1.0
    smart_trim_pre_roll_sec: float = 2.0
    smart_trim_end_post_roll_sec: float = 5.0

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.resolution_w, self.resolution_h)

    @property
    def resolution_str(self) -> str:
        return f"{self.resolution_w}x{self.resolution_h}"

    @property
    def activity_config(self) -> ActivityConfig:
        return build_activity_config(
            audio_threshold_db=self.activity_audio_threshold_db,
            video_diff_threshold=self.activity_video_diff_threshold,
            sample_interval_sec=self.activity_sample_interval_sec,
            sample_window_sec=self.activity_sample_window_sec,
            smart_trim_pre_roll_sec=self.smart_trim_pre_roll_sec,
            smart_trim_end_post_roll_sec=self.smart_trim_end_post_roll_sec,
        )


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
        smart_trim_enabled: bool | None = None,
        dynamic_extension_enabled: bool | None = None,
        dynamic_extension_idle_sec: int | None = None,
        dynamic_extension_max_sec: int | None = None,
    ) -> RuntimeRecordingConfig:
        """Return effective config using override > DB > env precedence."""
        values = {
            "resolution_w": self._setting_value("resolution_w", 1920),
            "resolution_h": self._setting_value("resolution_h", 1080),
            "recording_browser_mode": self._setting_value("recording_browser_mode", "app"),
            "recording_crop_mode": self._setting_value("recording_crop_mode", "off"),
            "recording_crop_top_px": self._setting_value("recording_crop_top_px", 0),
            "smart_trim_enabled": self._setting_value("smart_trim_enabled", True),
            "dynamic_extension_enabled": self._setting_value("dynamic_extension_enabled", True),
            "dynamic_extension_idle_sec": self._setting_value("dynamic_extension_idle_sec", 300),
            "dynamic_extension_max_sec": self._setting_value("dynamic_extension_max_sec", 3600),
            "activity_audio_threshold_db": self._setting_value("activity_audio_threshold_db", -45.0),
            "activity_video_diff_threshold": self._setting_value("activity_video_diff_threshold", 0.015),
            "activity_sample_interval_sec": self._setting_value("activity_sample_interval_sec", 5.0),
            "activity_sample_window_sec": self._setting_value("activity_sample_window_sec", 1.0),
            "smart_trim_pre_roll_sec": self._setting_value("smart_trim_pre_roll_sec", 2.0),
            "smart_trim_end_post_roll_sec": self._setting_value("smart_trim_end_post_roll_sec", 5.0),
            "lobby_wait_sec": self._setting_value("lobby_wait_sec", 900),
        }

        if db is not None:
            values.update(self._load_db_values(db))

        overrides = {
            "resolution_w": resolution_w,
            "resolution_h": resolution_h,
            "lobby_wait_sec": lobby_wait_sec,
            "smart_trim_enabled": smart_trim_enabled,
            "dynamic_extension_enabled": dynamic_extension_enabled,
            "dynamic_extension_idle_sec": dynamic_extension_idle_sec,
            "dynamic_extension_max_sec": dynamic_extension_max_sec,
        }
        for key, value in overrides.items():
            if value is not None:
                values[key] = value

        resolved_resolution_w = self._parse_positive_int("resolution_w", values["resolution_w"])
        resolved_resolution_h = self._parse_positive_int("resolution_h", values["resolution_h"])
        resolved_recording_crop_top_px = self._parse_int_range(
            "recording_crop_top_px",
            values["recording_crop_top_px"],
            minimum=0,
            maximum=resolved_resolution_h - 1,
        )
        resolved_recording_crop_mode = self._parse_crop_mode(values["recording_crop_mode"])
        resolved_recording_browser_mode = self._parse_browser_mode(values["recording_browser_mode"])
        resolved_lobby_wait_sec = self._parse_int_range(
            "lobby_wait_sec",
            values["lobby_wait_sec"],
            minimum=0,
            maximum=1800,
        )
        resolved_dynamic_extension_idle_sec = self._parse_int_range(
            "dynamic_extension_idle_sec",
            values["dynamic_extension_idle_sec"],
            minimum=1,
            maximum=14400,
        )
        resolved_dynamic_extension_max_sec = self._parse_int_range(
            "dynamic_extension_max_sec",
            values["dynamic_extension_max_sec"],
            minimum=0,
            maximum=86400,
        )
        if (
            resolved_dynamic_extension_max_sec > 0
            and resolved_dynamic_extension_max_sec < resolved_dynamic_extension_idle_sec
        ):
            raise RuntimeConfigError("dynamic_extension_max_sec must be 0 or greater than dynamic_extension_idle_sec")

        return RuntimeRecordingConfig(
            resolution_w=resolved_resolution_w,
            resolution_h=resolved_resolution_h,
            recording_browser_mode=resolved_recording_browser_mode,
            recording_crop_mode=resolved_recording_crop_mode,
            recording_crop_top_px=resolved_recording_crop_top_px,
            smart_trim_enabled=self._parse_bool("smart_trim_enabled", values["smart_trim_enabled"]),
            dynamic_extension_enabled=self._parse_bool(
                "dynamic_extension_enabled", values["dynamic_extension_enabled"]
            ),
            dynamic_extension_idle_sec=resolved_dynamic_extension_idle_sec,
            dynamic_extension_max_sec=resolved_dynamic_extension_max_sec,
            activity_audio_threshold_db=self._parse_float(
                "activity_audio_threshold_db", values["activity_audio_threshold_db"]
            ),
            activity_video_diff_threshold=self._parse_float_range(
                "activity_video_diff_threshold",
                values["activity_video_diff_threshold"],
                minimum=0.0,
                maximum=1.0,
            ),
            activity_sample_interval_sec=self._parse_float_range(
                "activity_sample_interval_sec",
                values["activity_sample_interval_sec"],
                minimum=1.0,
                maximum=300.0,
            ),
            activity_sample_window_sec=self._parse_float_range(
                "activity_sample_window_sec",
                values["activity_sample_window_sec"],
                minimum=0.25,
                maximum=30.0,
            ),
            smart_trim_pre_roll_sec=self._parse_float_range(
                "smart_trim_pre_roll_sec",
                values["smart_trim_pre_roll_sec"],
                minimum=0.0,
                maximum=60.0,
            ),
            smart_trim_end_post_roll_sec=self._parse_float_range(
                "smart_trim_end_post_roll_sec",
                values["smart_trim_end_post_roll_sec"],
                minimum=0.0,
                maximum=300.0,
            ),
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

    def _parse_float_range(self, key: str, value: object, *, minimum: float, maximum: float) -> float:
        parsed = self._parse_float(key, value)
        if parsed < minimum or parsed > maximum:
            raise RuntimeConfigError(f"{key} must be between {minimum} and {maximum}")
        return parsed

    def _parse_float(self, key: str, value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError(f"{key} must be a number") from exc

    def _parse_bool(self, key: str, value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise RuntimeConfigError(f"{key} must be a boolean")

    def _parse_crop_mode(self, value: object) -> str:
        mode = str(value).strip().lower()
        if mode not in VALID_RECORDING_CROP_MODES:
            choices = ", ".join(sorted(VALID_RECORDING_CROP_MODES))
            raise RuntimeConfigError(f"recording_crop_mode must be one of: {choices}")
        return mode

    def _parse_browser_mode(self, value: object) -> str:
        mode = str(value).strip().lower()
        if mode not in VALID_RECORDING_BROWSER_MODES:
            choices = ", ".join(sorted(VALID_RECORDING_BROWSER_MODES))
            raise RuntimeConfigError(f"recording_browser_mode must be one of: {choices}")
        return mode


def get_runtime_config_service(settings: Settings | None = None) -> RuntimeConfigService:
    """Create a runtime config resolver."""
    return RuntimeConfigService(settings=settings)
