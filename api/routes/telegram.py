"""Telegram API routes."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.models import TelegramUser, get_db

router = APIRouter(prefix="/telegram", tags=["telegram"])
settings = get_settings()


class TelegramUserResponse(BaseModel):
    """Telegram user response model."""

    id: int
    chat_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    approved: bool
    approved_by: str | None
    approved_at: str | None
    notify_on_start: bool
    notify_on_complete: bool
    notify_on_failure: bool
    notify_on_upload: bool
    created_at: str | None
    last_interaction_at: str | None

    class Config:
        from_attributes = True


class ApproveUserRequest(BaseModel):
    """Request to approve a user."""

    approved_by: str = "admin"


class UpdateNotificationsRequest(BaseModel):
    """Request to update notification preferences."""

    notify_on_start: bool | None = None
    notify_on_complete: bool | None = None
    notify_on_failure: bool | None = None
    notify_on_upload: bool | None = None


@router.get("/status")
async def get_telegram_status():
    """Get Telegram bot status."""
    configured = bool(settings.telegram_bot_token)

    return {
        "configured": configured,
        "bot_token_set": configured,
    }


@router.get("/users", response_model=list[TelegramUserResponse])
async def list_users(
    approved_only: bool = False,
    db: Session = Depends(get_db),
):
    """List all Telegram users."""
    query = db.query(TelegramUser).order_by(TelegramUser.created_at.desc())
    if approved_only:
        query = query.filter(TelegramUser.approved == True)
    users = query.all()
    return [TelegramUserResponse(**u.to_dict()) for u in users]


@router.get("/users/pending", response_model=list[TelegramUserResponse])
async def list_pending_users(db: Session = Depends(get_db)):
    """List users pending approval."""
    users = db.query(TelegramUser).filter(TelegramUser.approved == False).order_by(TelegramUser.created_at.desc()).all()
    return [TelegramUserResponse(**u.to_dict()) for u in users]


@router.post("/users/{user_id}/approve")
async def approve_user(
    user_id: int,
    request: ApproveUserRequest,
    db: Session = Depends(get_db),
):
    """Approve a Telegram user."""
    user = db.query(TelegramUser).filter(TelegramUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.approved = True
    user.approved_by = request.approved_by
    user.approved_at = datetime.utcnow()
    db.commit()

    # Send notification to user
    from telegram_bot.notifications import send_to_user

    await send_to_user(
        user.chat_id,
        "Your account has been approved! You can now use all bot commands.\n\nUse /help to see available commands.",
    )

    return {"success": True, "message": f"User {user.display_name} approved"}


@router.post("/users/{user_id}/revoke")
async def revoke_user(user_id: int, db: Session = Depends(get_db)):
    """Revoke a user's approval."""
    user = db.query(TelegramUser).filter(TelegramUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.approved = False
    user.approved_by = None
    user.approved_at = None
    db.commit()

    return {"success": True, "message": f"User {user.display_name} revoked"}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, db: Session = Depends(get_db)):
    """Delete a Telegram user."""
    user = db.query(TelegramUser).filter(TelegramUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(user)
    db.commit()

    return {"success": True, "message": "User deleted"}


@router.patch("/users/{user_id}/notifications")
async def update_notifications(
    user_id: int,
    request: UpdateNotificationsRequest,
    db: Session = Depends(get_db),
):
    """Update user notification preferences."""
    user = db.query(TelegramUser).filter(TelegramUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if request.notify_on_start is not None:
        user.notify_on_start = request.notify_on_start
    if request.notify_on_complete is not None:
        user.notify_on_complete = request.notify_on_complete
    if request.notify_on_failure is not None:
        user.notify_on_failure = request.notify_on_failure
    if request.notify_on_upload is not None:
        user.notify_on_upload = request.notify_on_upload

    db.commit()

    return {"success": True, "message": "Notification preferences updated"}
