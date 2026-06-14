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
    assert config.lobby_wait_sec == 600
    assert config.recordings_dir == Path(fake_settings.recordings_dir)
    assert config.diagnostics_dir == Path(fake_settings.diagnostics_dir)


def test_db_app_settings_override_env(fake_settings, db_session):
    set_app_setting(db_session, "resolution_w", "1280")
    set_app_setting(db_session, "resolution_h", "720")
    set_app_setting(db_session, "lobby_wait_sec", "300")

    config = RuntimeConfigService(settings=fake_settings).get_recording_config(db_session)

    assert config.resolution == (1280, 720)
    assert config.lobby_wait_sec == 300


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
