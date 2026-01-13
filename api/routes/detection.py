"""API routes for detection settings and logs."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.models import AppSettings, DetectionLog, get_db

router = APIRouter(prefix="/api/detection", tags=["detection"])


class DetectionConfigRequest(BaseModel):
    """Request model for detection configuration."""

    text_indicator_enabled: bool = True
    video_element_enabled: bool = True
    webrtc_connection_enabled: bool = True
    screen_freeze_enabled: bool = False
    audio_silence_enabled: bool = False
    url_change_enabled: bool = True
    screen_freeze_timeout_sec: int = 60
    min_detectors_agree: int = 1


@router.get("/config")
async def get_detection_config(db: Session = Depends(get_db)):
    """Get current detection configuration."""
    # Load from app_settings table
    settings_record = db.query(AppSettings).filter(AppSettings.key == "detection_config").first()

    if settings_record:
        config = json.loads(settings_record.value)
    else:
        # Return defaults
        config = {
            "text_indicator_enabled": True,
            "video_element_enabled": True,
            "webrtc_connection_enabled": True,
            "screen_freeze_enabled": False,
            "audio_silence_enabled": False,
            "url_change_enabled": True,
            "screen_freeze_timeout_sec": 60,
            "min_detectors_agree": 1,
        }

    return JSONResponse(content=config)


@router.post("/config")
async def save_detection_config(config: DetectionConfigRequest, db: Session = Depends(get_db)):
    """Save detection configuration."""
    config_json = json.dumps(config.model_dump())

    # Upsert into app_settings
    settings_record = db.query(AppSettings).filter(AppSettings.key == "detection_config").first()

    if settings_record:
        settings_record.value = config_json
        settings_record.updated_at = datetime.utcnow()
    else:
        settings_record = AppSettings(key="detection_config", value=config_json)
        db.add(settings_record)

    db.commit()

    return JSONResponse(content={"status": "ok", "message": "Configuration saved"})


@router.get("/logs")
async def get_detection_logs(
    db: Session = Depends(get_db),
    job_id: int | None = Query(None),
    limit: int = Query(100, le=1000),
    offset: int = Query(0),
):
    """Get detection logs with optional filtering."""
    query = db.query(DetectionLog).order_by(DetectionLog.triggered_at.desc())

    if job_id:
        query = query.filter(DetectionLog.job_id == job_id)

    total = query.count()
    logs = query.offset(offset).limit(limit).all()

    return JSONResponse(
        content={
            "total": total,
            "limit": limit,
            "offset": offset,
            "logs": [log.to_dict() for log in logs],
        }
    )


@router.get("/logs/export")
async def export_detection_logs(
    db: Session = Depends(get_db),
    job_id: int | None = Query(None),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """Export detection logs as JSON or CSV."""
    query = db.query(DetectionLog).order_by(DetectionLog.triggered_at.desc())

    if job_id:
        query = query.filter(DetectionLog.job_id == job_id)

    logs = query.all()
    data = [log.to_dict() for log in logs]

    if format == "csv":
        # Generate CSV
        import csv
        import io

        output = io.StringIO()
        if data:
            writer = csv.DictWriter(output, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)

        content = output.getvalue()
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=detection_logs.csv"},
        )
    else:
        # JSON
        return JSONResponse(content=data)


@router.post("/logs/{log_id}/mark-accuracy")
async def mark_detection_accuracy(
    log_id: int,
    was_accurate: bool,
    db: Session = Depends(get_db),
):
    """Mark a detection log entry as accurate or inaccurate for training."""
    log = db.query(DetectionLog).filter(DetectionLog.id == log_id).first()

    if not log:
        return JSONResponse(status_code=404, content={"error": "Log not found"})

    log.was_accurate = was_accurate
    db.commit()

    return JSONResponse(content={"status": "ok"})


@router.delete("/logs")
async def clear_detection_logs(
    db: Session = Depends(get_db),
    job_id: int | None = Query(None),
):
    """Clear detection logs."""
    query = db.query(DetectionLog)

    if job_id:
        query = query.filter(DetectionLog.job_id == job_id)

    deleted = query.delete()
    db.commit()

    return JSONResponse(content={"status": "ok", "deleted_count": deleted})
