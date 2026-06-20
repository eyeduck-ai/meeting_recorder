from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown env vars like DEBUG_VNC
    )

    # Timezone
    tz: str = "Asia/Taipei"

    @property
    def timezone(self) -> str:
        """Return timezone string."""
        return self.tz

    # Recording settings
    resolution_w: int = 1920
    resolution_h: int = 1080
    recording_browser_mode: Literal["app", "normal"] = "app"
    recording_crop_mode: Literal["auto", "manual", "off"] = "off"
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
    lobby_wait_sec: int = 900
    max_recording_sec: int = 14400  # 4 hours default max
    max_concurrent_recordings: int = 2
    recording_display_start: int = 100
    recording_display_pool_size: int = 16
    min_free_disk_gb_before_recording: float = 10.0

    # Jitsi settings
    jitsi_base_url: str = "https://meet.jit.si/"

    # Database
    database_url: str = "sqlite:///./data/app.db"

    # FFmpeg
    ffmpeg_preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = "ultrafast"
    ffmpeg_crf: int = 23
    ffmpeg_audio_bitrate: str = "128k"
    ffmpeg_thread_queue_size: int = 1024
    ffmpeg_audio_filter: str = "aresample=async=1000:first_pts=0"
    ffmpeg_debug_ts: bool = False
    ffmpeg_stop_grace_sec: int = 5
    ffmpeg_sigint_timeout_sec: int = 8
    ffmpeg_sigterm_timeout_sec: int = 5
    ffmpeg_stall_timeout_sec: int = 120
    ffmpeg_stall_grace_sec: int = 30
    ffmpeg_transcode_on_upload: bool = False
    ffmpeg_transcode_preset: Literal[
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    ] = "slow"
    ffmpeg_transcode_crf: int = 30
    ffmpeg_transcode_audio_bitrate: str = "96k"
    ffmpeg_transcode_video_bitrate: str | None = "1500k"
    max_parallel_transcodes: int = 1

    # Paths
    recordings_dir: Path = Path("./recordings")
    diagnostics_dir: Path = Path("./diagnostics")
    data_dir: Path = Path("./data")
    logs_dir: Path = Path("./logs")

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_allowed_origins: str = ""

    # Authentication (simple password protection)
    auth_password: str | None = None  # Set via AUTH_PASSWORD env var
    auth_session_secret: str = "change-me-in-production"  # Secret for session signing
    auth_session_max_age: int = 86400  # 24 hours

    # Telegram (Phase 5)
    telegram_bot_token: str | None = None
    telegram_webhook_url: str | None = None

    # YouTube (Phase 4)
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_default_privacy: Literal["public", "private", "unlisted"] = "unlisted"
    youtube_upload_chunk_size: int = 10 * 1024 * 1024  # 10MB
    youtube_max_retries: int = 5

    @model_validator(mode="after")
    def validate_recording_capacity(self) -> "Settings":
        """Validate concurrency settings that must agree with each other."""
        if self.max_concurrent_recordings < 1:
            raise ValueError("MAX_CONCURRENT_RECORDINGS must be >= 1")
        if self.recording_display_pool_size < 1:
            raise ValueError("RECORDING_DISPLAY_POOL_SIZE must be >= 1")
        if self.max_concurrent_recordings > self.recording_display_pool_size:
            raise ValueError(
                "MAX_CONCURRENT_RECORDINGS must be <= RECORDING_DISPLAY_POOL_SIZE "
                f"({self.max_concurrent_recordings} > {self.recording_display_pool_size})"
            )
        return self

    @property
    def resolution(self) -> tuple[int, int]:
        """Return resolution as (width, height) tuple."""
        return (self.resolution_w, self.resolution_h)

    @property
    def resolution_str(self) -> str:
        """Return resolution as WxH string for FFmpeg."""
        return f"{self.resolution_w}x{self.resolution_h}"

    @property
    def youtube_configured(self) -> bool:
        """Check if YouTube credentials are configured."""
        return bool(self.youtube_client_id and self.youtube_client_secret)

    @property
    def cors_allowed_origin_list(self) -> list[str]:
        """Return explicitly allowed CORS origins."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
