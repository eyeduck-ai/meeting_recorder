from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.operations import OperationsService

router = APIRouter(tags=["Operations"])


@router.get("/operations")
async def operations_summary(request: Request):
    return await OperationsService().snapshot(request.app)


class RetryNotificationRequest(BaseModel):
    confirmed_absent: bool = False


@router.post("/operations/notifications/{delivery_id}/retry")
async def retry_notification(delivery_id: int, body: RetryNotificationRequest):
    from telegram_bot.notifications import get_notification_delivery_service

    try:
        queued = get_notification_delivery_service().retry_failed(delivery_id, confirmed_absent=body.confirmed_absent)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not queued:
        raise HTTPException(status_code=409, detail="Notification is missing, stale, or already acknowledged")
    return {"queued": True}


@router.post("/operations/work/{job_id}/{kind}/retry")
async def retry_work(job_id: str, kind: str, request: Request):
    if kind not in {"post_process", "upload"}:
        raise HTTPException(status_code=400, detail="Unknown work kind")
    if not request.app.state.job_runner.retry_durable_work(job_id, kind):
        raise HTTPException(
            status_code=409, detail="Only failed work can be retried; needs_review requires reconciliation"
        )
    return {"queued": True}
