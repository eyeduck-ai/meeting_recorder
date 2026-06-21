import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import services.secrets as secrets_module
from api.routes.meetings import _to_response
from api.routes.recording_management import NotificationConfigRequest, get_notification_config, save_notification_config
from database.models import AppSettings, Base, Meeting
from services.secrets import SECRET_MASK
from uploading.youtube import OAuthToken, TokenStorage, YouTubeUploader
from utils.timezone import utc_now


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'secrets.db'}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_meeting_response_exposes_only_password_presence():
    meeting = Meeting(
        id=1,
        name="Secret Meeting",
        provider="jitsi",
        meeting_code="room",
        meeting_password_plaintext="raw-secret",
        default_display_name="Recorder Bot",
    )

    response = _to_response(meeting)
    payload = response.model_dump()

    assert payload["has_password"] is True
    assert "password" not in payload
    assert "meeting_password_plaintext" not in payload
    assert "raw-secret" not in json.dumps(payload)


def test_secret_module_does_not_expose_mask_detection_helper():
    assert not hasattr(secrets_module, "is_masked_secret")


@pytest.mark.asyncio
async def test_notification_config_masks_and_preserves_secrets(db_session):
    db_session.add(
        AppSettings(
            key="notification_config",
            value=json.dumps(
                {
                    "smtp_enabled": True,
                    "smtp_host": "smtp.example.com",
                    "smtp_password": "smtp-secret",
                    "webhook_enabled": True,
                    "webhook_url": "https://hooks.example.com",
                    "webhook_secret": "hook-secret",
                }
            ),
        )
    )
    db_session.commit()

    response = await get_notification_config(db_session)
    payload = json.loads(response.body)
    assert payload["smtp_password"] == SECRET_MASK
    assert payload["webhook_secret"] == SECRET_MASK

    payload["smtp_password"] = SECRET_MASK
    payload["webhook_secret"] = SECRET_MASK
    await save_notification_config(NotificationConfigRequest(**payload), db_session)

    record = db_session.query(AppSettings).filter(AppSettings.key == "notification_config").first()
    stored = json.loads(record.value)
    assert stored["smtp_password"] == "smtp-secret"
    assert stored["webhook_secret"] == "hook-secret"

    payload["smtp_password"] = ""
    payload["webhook_secret"] = ""
    await save_notification_config(NotificationConfigRequest(**payload), db_session)
    db_session.refresh(record)
    stored = json.loads(record.value)
    assert stored["smtp_password"] == ""
    assert stored["webhook_secret"] == ""


@pytest.mark.asyncio
async def test_notification_config_mask_without_existing_secret_saves_empty(db_session):
    payload = {
        "smtp_password": SECRET_MASK,
        "webhook_secret": SECRET_MASK,
    }

    await save_notification_config(NotificationConfigRequest(**payload), db_session)

    record = db_session.query(AppSettings).filter(AppSettings.key == "notification_config").first()
    stored = json.loads(record.value)
    assert stored["smtp_password"] == ""
    assert stored["webhook_secret"] == ""


def test_token_storage_round_trip_and_restricts_permissions(tmp_path, monkeypatch):
    token_path = tmp_path / "youtube_token.json"
    chmod_calls = []

    def fake_chmod(self, mode):
        chmod_calls.append((self, mode))

    monkeypatch.setattr(type(token_path), "chmod", fake_chmod)

    storage = TokenStorage(storage_path=token_path)
    token = OAuthToken(
        access_token="access",
        refresh_token="refresh",
        expires_at=utc_now() + timedelta(hours=1),
    )

    storage.save(token)
    loaded = storage.load()

    assert loaded.access_token == "access"
    assert loaded.refresh_token == "refresh"
    assert chmod_calls == [(token_path, 0o600)]


@pytest.mark.asyncio
async def test_youtube_uploader_reads_upload_chunks_via_thread(tmp_path, monkeypatch):
    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"abcdef")
    to_thread_calls = []

    async def fake_to_thread(func, *args):
        to_thread_calls.append((func, args))
        return func(*args)

    monkeypatch.setattr("uploading.youtube.asyncio.to_thread", fake_to_thread)

    uploader = YouTubeUploader(
        client_id="client",
        client_secret="secret",
        token_storage=TokenStorage(storage_path=tmp_path / "token.json"),
    )

    chunk = await uploader._read_chunk(video_path, 2, 3)

    assert chunk == b"cde"
    assert len(to_thread_calls) == 1
