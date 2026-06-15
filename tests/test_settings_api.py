"""Tests for editable settings API behavior."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.settings import SettingsUpdate, get_settings_endpoint, update_settings_endpoint
from database.models import AppSettings, Base


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'settings-api.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_settings_endpoint_returns_recording_crop_top(db_session):
    db_session.add(AppSettings(key="recording_browser_mode", value="normal"))
    db_session.add(AppSettings(key="recording_crop_mode", value="manual"))
    db_session.add(AppSettings(key="recording_crop_top_px", value="42"))
    db_session.commit()

    response = get_settings_endpoint(db_session)

    assert response["recording_browser_mode"] == "normal"
    assert response["recording_crop_mode"] == "manual"
    assert response["recording_crop_top_px"] == 42
    assert response["smart_trim_enabled"] is True
    assert response["dynamic_extension_idle_sec"] == 300


def test_settings_update_accepts_valid_recording_crop_top(db_session):
    response = update_settings_endpoint(
        SettingsUpdate(
            resolution_h=720,
            recording_browser_mode="normal",
            recording_crop_mode="manual",
            recording_crop_top_px=64,
            smart_trim_enabled=False,
            dynamic_extension_idle_sec=300,
            dynamic_extension_max_sec=3600,
            activity_audio_threshold_db=-42.0,
        ),
        db_session,
    )

    assert response["resolution_h"] == 720
    assert response["recording_browser_mode"] == "normal"
    assert response["recording_crop_mode"] == "manual"
    assert response["recording_crop_top_px"] == 64
    assert response["smart_trim_enabled"] is False
    assert response["dynamic_extension_idle_sec"] == 300
    assert response["dynamic_extension_max_sec"] == 3600
    assert response["activity_audio_threshold_db"] == -42.0


def test_settings_update_rejects_crop_equal_to_resolution_height(db_session):
    with pytest.raises(HTTPException) as exc:
        update_settings_endpoint(
            SettingsUpdate(resolution_h=720, recording_crop_top_px=720),
            db_session,
        )

    assert exc.value.status_code == 422
    assert "recording_crop_top_px" in exc.value.detail


def test_settings_update_rejects_invalid_recording_crop_mode():
    with pytest.raises(ValidationError):
        SettingsUpdate(recording_crop_mode="dynamic")


def test_settings_update_rejects_invalid_recording_browser_mode():
    with pytest.raises(ValidationError):
        SettingsUpdate(recording_browser_mode="popup")


def test_settings_update_rejects_extension_max_below_idle(db_session):
    with pytest.raises(HTTPException) as exc:
        update_settings_endpoint(
            SettingsUpdate(dynamic_extension_idle_sec=300, dynamic_extension_max_sec=120),
            db_session,
        )

    assert exc.value.status_code == 422
    assert "dynamic_extension_max_sec" in exc.value.detail
