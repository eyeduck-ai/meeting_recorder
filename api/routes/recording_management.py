"""API routes for recording management."""

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.models import AppSettings, get_db
from services.recording_manager import get_recording_manager
from utils.timezone import utc_now

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


@router.get("/list")
async def list_recordings(
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    order_by: str = Query("newest", pattern="^(newest|oldest|largest|smallest)$"),
):
    """List recordings with metadata."""
    manager = get_recording_manager()
    recordings = manager.list_recordings(limit=limit, offset=offset, order_by=order_by)
    return JSONResponse(
        content={
            "recordings": recordings,
            "count": len(recordings),
        }
    )


@router.get("/disk-usage")
async def get_disk_usage():
    """Get disk usage statistics."""
    manager = get_recording_manager()
    usage = manager.get_disk_usage()
    return JSONResponse(content=usage)


@router.post("/generate-thumbnail")
async def generate_thumbnail(video_path: str):
    """Generate thumbnail for a video."""
    manager = get_recording_manager()
    thumbnail_path = await manager.generate_thumbnail(video_path)

    if thumbnail_path:
        return JSONResponse(
            content={
                "status": "ok",
                "thumbnail_path": thumbnail_path,
            }
        )
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to generate thumbnail"},
        )


@router.post("/cleanup")
async def cleanup_recordings(
    max_age_days: int = Query(30, ge=1),
    max_count: int | None = Query(None, ge=1),
    dry_run: bool = Query(True),
):
    """Clean up old recordings."""
    manager = get_recording_manager()
    result = await manager.cleanup_old_recordings(
        max_age_days=max_age_days,
        max_count=max_count,
        dry_run=dry_run,
    )
    return JSONResponse(content=result)


@router.get("/check-disk")
async def check_disk_space(
    threshold_gb: float = Query(10.0, ge=1.0),
    auto_cleanup: bool = Query(False),
):
    """Check disk space and optionally trigger cleanup."""
    manager = get_recording_manager()
    result = await manager.check_disk_space(
        threshold_gb=threshold_gb,
        auto_cleanup=auto_cleanup,
    )
    return JSONResponse(content=result)


# Notification config API
class NotificationConfigRequest(BaseModel):
    """Request model for notification configuration."""

    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: list[str] = []
    smtp_use_tls: bool = True
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_secret: str = ""


@router.get("/notification-config")
async def get_notification_config(db: Session = Depends(get_db)):
    """Get notification configuration."""
    record = db.query(AppSettings).filter(AppSettings.key == "notification_config").first()

    if record:
        config = json.loads(record.value)
        # Mask password
        if config.get("smtp_password"):
            config["smtp_password"] = "********"
    else:
        config = {
            "smtp_enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
            "smtp_to": [],
            "smtp_use_tls": True,
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_secret": "",
        }

    return JSONResponse(content=config)


@router.post("/notification-config")
async def save_notification_config(
    config: NotificationConfigRequest,
    db: Session = Depends(get_db),
):
    """Save notification configuration."""
    # Check if password is masked (not changed)
    existing_record = db.query(AppSettings).filter(AppSettings.key == "notification_config").first()

    config_dict = config.model_dump()

    # Preserve existing password if masked
    if config_dict.get("smtp_password") == "********" and existing_record:
        existing_config = json.loads(existing_record.value)
        config_dict["smtp_password"] = existing_config.get("smtp_password", "")

    config_json = json.dumps(config_dict)

    if existing_record:
        existing_record.value = config_json
        existing_record.updated_at = utc_now()
    else:
        record = AppSettings(key="notification_config", value=config_json)
        db.add(record)

    db.commit()

    # Reload notification service by resetting the global instance
    import services.notification as notification_module

    notification_module._notification_service = None

    return JSONResponse(content={"status": "ok", "message": "Configuration saved"})


@router.post("/test-email")
async def test_email_notification(db: Session = Depends(get_db)):
    """Send a test email notification."""
    from services.notification import get_notification_service

    service = get_notification_service()

    if not service.config.smtp_enabled:
        return JSONResponse(
            status_code=400,
            content={"error": "Email notifications are not enabled"},
        )

    success = await service.email.send(
        subject="🧪 Test Email from Meeting Recorder",
        body="This is a test email from your Meeting Recorder system.\n\nIf you received this, email notifications are working correctly!",
    )

    if success:
        return JSONResponse(content={"status": "ok", "message": "Test email sent"})
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to send test email"},
        )


@router.post("/test-webhook")
async def test_webhook_notification(db: Session = Depends(get_db)):
    """Send a test webhook notification."""
    from services.notification import get_notification_service

    service = get_notification_service()

    if not service.config.webhook_enabled:
        return JSONResponse(
            status_code=400,
            content={"error": "Webhook notifications are not enabled"},
        )

    success = await service.webhook.send(
        event="test",
        payload={"message": "Test webhook from Meeting Recorder"},
    )

    if success:
        return JSONResponse(content={"status": "ok", "message": "Test webhook sent"})
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to send test webhook"},
        )
