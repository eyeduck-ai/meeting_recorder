"""API routes for detection settings and logs."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from database.models import DetectionLog
from database.session import get_db

router = APIRouter(prefix="/api/detection", tags=["detection"])


def _apply_log_filters(query, *, job_id: int | None, detector_type: str | None, detected: bool | None):
    """Apply shared detection log filters to a SQLAlchemy query."""
    if job_id is not None:
        query = query.filter(DetectionLog.job_id == job_id)
    if detector_type:
        query = query.filter(DetectionLog.detector_type == detector_type)
    if detected is not None:
        query = query.filter(DetectionLog.detected.is_(detected))
    return query


def _summary_count(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _detection_log_dict(log: DetectionLog) -> dict:
    """Build the detection log API/export payload."""
    return {
        "id": log.id,
        "job_id": log.job_id,
        "detector_type": log.detector_type,
        "detected": log.detected,
        "confidence": log.confidence,
        "reason": log.reason,
        "attempt_no": log.attempt_no,
        "was_accurate": log.was_accurate,
        "triggered_at": log.triggered_at.isoformat() if log.triggered_at else None,
    }


@router.get("/logs")
async def get_detection_logs(
    db: Session = Depends(get_db),
    job_id: int | None = Query(None),
    detector_type: str | None = Query(None),
    detected: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get activity/detection logs with optional filtering."""
    base_query = _apply_log_filters(
        db.query(DetectionLog),
        job_id=job_id,
        detector_type=detector_type,
        detected=detected,
    )
    total = base_query.count()
    summary_row = _apply_log_filters(
        db.query(
            _summary_count(DetectionLog.detected.is_(True)).label("triggered"),
            _summary_count(DetectionLog.was_accurate.is_(True)).label("accurate"),
            _summary_count(DetectionLog.was_accurate.is_(False)).label("inaccurate"),
        ),
        job_id=job_id,
        detector_type=detector_type,
        detected=detected,
    ).one()
    logs = base_query.order_by(DetectionLog.triggered_at.desc()).offset(offset).limit(limit).all()

    return JSONResponse(
        content={
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": {
                "triggered": int(summary_row.triggered or 0),
                "accurate": int(summary_row.accurate or 0),
                "inaccurate": int(summary_row.inaccurate or 0),
            },
            "logs": [_detection_log_dict(log) for log in logs],
        }
    )


@router.get("/logs/export")
async def export_detection_logs(
    db: Session = Depends(get_db),
    job_id: int | None = Query(None),
    detector_type: str | None = Query(None),
    detected: bool | None = Query(None),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """Export activity/detection logs as JSON or CSV."""
    logs = (
        _apply_log_filters(
            db.query(DetectionLog),
            job_id=job_id,
            detector_type=detector_type,
            detected=detected,
        )
        .order_by(DetectionLog.triggered_at.desc())
        .all()
    )
    data = [_detection_log_dict(log) for log in logs]

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
