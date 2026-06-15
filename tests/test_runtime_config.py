"""Tests for runtime recording configuration resolution."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AppSettings, Base
from services.runtime_config import RuntimeConfigError, RuntimeConfigService


@pytest.fixture
def fake_settings(tmp_path):
    return SimpleNamespace(
        resolution_w=1024,
        resolution_h=768,
        recording_browser_mode="app",
        recording_crop_mode="off",
        recording_crop_top_px=16,
        lobby_wait_sec=600,
        recordings_dir=tmp_path / "env-recordings",
        diagnostics_dir=tmp_path / "env-diagnostics",
        ffmpeg_stall_timeout_sec=120,
        ffmpeg_stall_grace_sec=30,
    )


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime-config.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def set_app_setting(db_session, key: str, value: str) -> None:
    db_session.add(AppSettings(key=key, value=value))
    db_session.commit()


def test_env_only_fallback(fake_settings):
    config = RuntimeConfigService(settings=fake_settings).get_recording_config()

    assert config.resolution == (1024, 768)
    assert config.recording_browser_mode == "app"
    assert config.recording_crop_mode == "off"
    assert config.recording_crop_top_px == 16
    assert config.lobby_wait_sec == 600
    assert config.recordings_dir == Path(fake_settings.recordings_dir)
    assert config.diagnostics_dir == Path(fake_settings.diagnostics_dir)


def test_db_app_settings_override_env(fake_settings, db_session):
    set_app_setting(db_session, "resolution_w", "1280")
    set_app_setting(db_session, "resolution_h", "720")
    set_app_setting(db_session, "recording_browser_mode", "normal")
    set_app_setting(db_session, "recording_crop_mode", "manual")
    set_app_setting(db_session, "recording_crop_top_px", "48")
    set_app_setting(db_session, "lobby_wait_sec", "300")

    config = RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)

    assert config.resolution == (1280, 720)
    assert config.recording_browser_mode == "normal"
    assert config.recording_crop_mode == "manual"
    assert config.recording_crop_top_px == 48
    assert config.lobby_wait_sec == 300


def test_db_activity_settings_override_defaults(fake_settings, db_session):
    set_app_setting(db_session, "smart_trim_enabled", "false")
    set_app_setting(db_session, "dynamic_extension_enabled", "true")
    set_app_setting(db_session, "dynamic_extension_idle_sec", "300")
    set_app_setting(db_session, "dynamic_extension_max_sec", "3600")
    set_app_setting(db_session, "activity_audio_threshold_db", "-42.5")
    set_app_setting(db_session, "activity_video_diff_threshold", "0.02")

    config = RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)

    assert config.smart_trim_enabled is False
    assert config.dynamic_extension_enabled is True
    assert config.dynamic_extension_idle_sec == 300
    assert config.dynamic_extension_max_sec == 3600
    assert config.activity_config.audio_threshold_db == -42.5
    assert config.activity_config.video_diff_threshold == 0.02


def test_explicit_override_wins_over_db(fake_settings, db_session):
    set_app_setting(db_session, "resolution_w", "1280")
    set_app_setting(db_session, "resolution_h", "720")
    set_app_setting(db_session, "lobby_wait_sec", "300")

    config = RuntimeConfigService(settings=fake_settings).get_recording_config(
        db_session,
        resolution_w=1920,
        resolution_h=1080,
        lobby_wait_sec=60,
    )

    assert config.resolution == (1920, 1080)
    assert config.lobby_wait_sec == 60


def test_explicit_activity_override_wins_over_db(fake_settings, db_session):
    set_app_setting(db_session, "smart_trim_enabled", "true")
    set_app_setting(db_session, "dynamic_extension_enabled", "true")
    set_app_setting(db_session, "dynamic_extension_idle_sec", "300")
    set_app_setting(db_session, "dynamic_extension_max_sec", "3600")

    config = RuntimeConfigService(settings=fake_settings).get_recording_config(
        db_session,
        smart_trim_enabled=False,
        dynamic_extension_enabled=False,
        dynamic_extension_idle_sec=600,
        dynamic_extension_max_sec=1800,
    )

    assert config.smart_trim_enabled is False
    assert config.dynamic_extension_enabled is False
    assert config.dynamic_extension_idle_sec == 600
    assert config.dynamic_extension_max_sec == 1800


def test_omitted_manual_lobby_wait_uses_db_default(fake_settings, db_session):
    set_app_setting(db_session, "lobby_wait_sec", "450")

    config = RuntimeConfigService(settings=fake_settings).get_recording_config(
        db_session,
        lobby_wait_sec=None,
    )

    assert config.lobby_wait_sec == 450


def test_invalid_db_numeric_config_fails_explicitly(fake_settings, db_session):
    set_app_setting(db_session, "resolution_w", "wide")

    with pytest.raises(RuntimeConfigError, match="resolution_w"):
        RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)


def test_lobby_wait_range_is_validated(fake_settings, db_session):
    set_app_setting(db_session, "lobby_wait_sec", "1801")

    with pytest.raises(RuntimeConfigError, match="lobby_wait_sec"):
        RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)


def test_recording_crop_top_must_be_smaller_than_resolution_height(fake_settings, db_session):
    set_app_setting(db_session, "resolution_h", "720")
    set_app_setting(db_session, "recording_crop_top_px", "720")

    with pytest.raises(RuntimeConfigError, match="recording_crop_top_px"):
        RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)


def test_recording_crop_mode_must_be_valid(fake_settings, db_session):
    set_app_setting(db_session, "recording_crop_mode", "dynamic")

    with pytest.raises(RuntimeConfigError, match="recording_crop_mode"):
        RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)


def test_recording_browser_mode_must_be_valid(fake_settings, db_session):
    set_app_setting(db_session, "recording_browser_mode", "popup")

    with pytest.raises(RuntimeConfigError, match="recording_browser_mode"):
        RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)


def test_dynamic_extension_max_must_not_be_below_idle(fake_settings, db_session):
    set_app_setting(db_session, "dynamic_extension_idle_sec", "300")
    set_app_setting(db_session, "dynamic_extension_max_sec", "120")

    with pytest.raises(RuntimeConfigError, match="dynamic_extension_max_sec"):
        RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)
