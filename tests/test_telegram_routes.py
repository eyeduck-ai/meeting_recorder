"""Tests for Telegram API routes."""

from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import telegram
from database.models import Base, TelegramUser
from database.session import get_db


def test_telegram_status_reads_current_settings(monkeypatch):
    app = FastAPI()
    app.include_router(telegram.router)
    client = TestClient(app)

    monkeypatch.setattr(telegram, "get_settings", lambda: SimpleNamespace(telegram_bot_token="token"))

    response = client.get("/telegram/status")

    assert response.status_code == 200
    assert response.json() == {"configured": True, "bot_token_set": True}
    assert not hasattr(telegram, "settings")


def _telegram_client_with_users(users: list[TelegramUser]) -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        db.add_all(users)
        db.commit()

    app = FastAPI()
    app.include_router(telegram.router)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_telegram_users_route_uses_explicit_response_mapping():
    client = _telegram_client_with_users(
        [
            TelegramUser(
                chat_id=1001,
                username="approved_user",
                first_name="Approved",
                last_name="User",
                approved=True,
                approved_by="admin",
                approved_at=datetime(2026, 1, 2, 3, 4, 5),
                notify_on_start=True,
                notify_on_complete=False,
                notify_on_failure=True,
                notify_on_upload=False,
                created_at=datetime(2026, 1, 1, 1, 2, 3),
                last_interaction_at=datetime(2026, 1, 3, 4, 5, 6),
            )
        ]
    )

    response = client.get("/telegram/users")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 1,
            "chat_id": 1001,
            "username": "approved_user",
            "first_name": "Approved",
            "last_name": "User",
            "approved": True,
            "approved_by": "admin",
            "approved_at": "2026-01-02T03:04:05",
            "notify_on_start": True,
            "notify_on_complete": False,
            "notify_on_failure": True,
            "notify_on_upload": False,
            "created_at": "2026-01-01T01:02:03",
            "last_interaction_at": "2026-01-03T04:05:06",
        }
    ]


def test_telegram_pending_users_route_filters_and_maps_users():
    client = _telegram_client_with_users(
        [
            TelegramUser(chat_id=1001, approved=True, created_at=datetime(2026, 1, 1)),
            TelegramUser(chat_id=1002, approved=False, created_at=datetime(2026, 1, 2)),
        ]
    )

    response = client.get("/telegram/users/pending")

    assert response.status_code == 200
    assert [user["chat_id"] for user in response.json()] == [1002]
