"""Durable, per-recipient Telegram outbox with bounded, ordered delivery.

Telegram has no idempotency key for sendMessage. An interrupted/ambiguous new
send is therefore retained as uncertain, not automatically sent a second time.
Edits to a known message are idempotent and may safely be retried.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from weakref import WeakValueDictionary

from sqlalchemy import or_

from database.models import RecordingJob, TelegramUser
from database.notification_models import NotificationDelivery
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 5
_STAGE_PHASES = {"started", "starting", "joining", "waiting_lobby", "recording", "finalizing", "retry"}
_PREFERENCES = {
    "start": "notify_on_start",
    "complete": "notify_on_complete",
    "failure": "notify_on_failure",
    "upload": "notify_on_upload",
}
_PHASE_RANK = {
    "started": 1,
    "starting": 1,
    "joining": 2,
    "waiting_lobby": 3,
    "recording": 4,
    "finalizing": 5,
    "retry": 6,
    "completed": 10,
    "uploading": 10,
    "failed": 10,
    "uploaded": 20,
}


@dataclass(frozen=True)
class DeliveryResult:
    state: str
    message_id: int | None = None
    error: str | None = None
    retry_after: float | None = None


class NotificationDeliveryService:
    def __init__(self, session_factory, sender: Callable, *, concurrency: int = 3):
        self._session_factory = session_factory
        self._sender = sender
        self._semaphore = asyncio.Semaphore(concurrency)
        self._locks: WeakValueDictionary[tuple[str, int], asyncio.Lock] = WeakValueDictionary()
        self._task: asyncio.Task | None = None
        self._inflight: Counter[int] = Counter()

    def list_deliveries(self, job_id: str | None = None, limit: int = 100) -> list[dict]:
        """Return diagnostic metadata only, never Telegram text or credentials."""
        with self._session_factory() as db:
            query = db.query(NotificationDelivery)
            if job_id is not None:
                query = query.filter(NotificationDelivery.job_id == job_id)
            rows = query.order_by(NotificationDelivery.id.desc()).limit(max(1, min(limit, 500))).all()
            return [
                {
                    name: ensure_utc(getattr(row, name))
                    if isinstance(getattr(row, name), datetime)
                    else getattr(row, name)
                    for name in (
                        "id",
                        "job_id",
                        "event_key",
                        "chat_id",
                        "notification_type",
                        "phase",
                        "state",
                        "message_id",
                        "attempt_count",
                        "created_at",
                        "updated_at",
                        "acknowledged_at",
                        "next_attempt_at",
                        "last_error",
                    )
                }
                for row in rows
            ]

    async def start(self):
        if self._task is not None:
            return
        self._recover_unowned_sends()
        self._task = asyncio.create_task(self._run(), name="telegram-notification-outbox")

    def _recover_unowned_sends(self):
        """A DB failure after network I/O must not leave an event stuck until restart."""
        with self._session_factory() as db:
            for row in db.query(NotificationDelivery).filter_by(state="sending").all():
                if self._inflight[row.id]:
                    continue
                row.state = "pending" if row.operation == "edit" and row.message_id else "uncertain"
                row.last_error = "delivery_interrupted"
                row.next_attempt_at = utc_now() if row.state == "pending" else None
            db.commit()

    async def shutdown(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self):
        while True:
            try:
                await self.retry_due()
            except Exception as exc:
                logger.error("Telegram outbox retry iteration failed: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def enqueue(
        self,
        *,
        job_id: str,
        phase: str,
        notification_type: str,
        attempt_no: int,
        message: str,
        chat_ids: list[int],
        event_reference: str | None = None,
    ) -> int | None:
        """Commit intents before any network I/O; return first ack for legacy callers."""
        event_key = f"{attempt_no}:{phase}" + (f":{event_reference}" if event_reference else "")
        ids = []
        with self._session_factory() as db:
            if not self._job_allows(db, job_id, phase, attempt_no, event_reference):
                return None
            # Supersede old stages even when the terminal preference has no recipients.
            existing = db.query(NotificationDelivery).filter_by(job_id=job_id).all()
            for row in existing:
                if row.state in {"pending", "sending"} and (
                    row.attempt_no < attempt_no
                    or (row.attempt_no == attempt_no and _PHASE_RANK.get(phase, 0) > _PHASE_RANK.get(row.phase, 0))
                    or (phase == "uploaded" and row.phase == phase and row.event_reference != event_reference)
                ):
                    row.state = "superseded"
                    row.payload_text = ""
                    row.next_attempt_at = None
            by_chat = {row.chat_id: row for row in existing if row.event_key == event_key}
            for chat_id in dict.fromkeys(chat_ids):
                row = by_chat.get(chat_id)
                if row is None:
                    row = NotificationDelivery(
                        job_id=job_id,
                        event_key=event_key,
                        chat_id=chat_id,
                        notification_type=notification_type,
                        phase=phase,
                        event_reference=event_reference,
                        attempt_no=attempt_no,
                        payload_text=message,
                        state="pending",
                        next_attempt_at=utc_now(),
                    )
                    db.add(row)
                    db.flush()
                ids.append(row.id)
            db.commit()
        results = await asyncio.gather(*(self._deliver(delivery_id) for delivery_id in ids), return_exceptions=True)
        for value in results:
            if isinstance(value, Exception):
                logger.error("Telegram outbox dispatch failed: %s", type(value).__name__)
        acknowledged = [value for value in results if isinstance(value, int)]
        if len(acknowledged) != len(ids):
            logger.warning(
                "Telegram notification incomplete for job %s type=%s: acknowledged=%s recipients=%s",
                job_id,
                notification_type,
                len(acknowledged),
                len(ids),
            )
        return acknowledged[0] if acknowledged else None

    def _current(self, db, row: NotificationDelivery) -> bool:
        user = db.query(TelegramUser).filter_by(chat_id=row.chat_id).first()
        if user is None or not user.approved or not getattr(user, _PREFERENCES[row.notification_type]):
            return False
        if not self._job_allows(db, row.job_id, row.phase, row.attempt_no, row.event_reference):
            return False
        latest_terminal = (
            db.query(NotificationDelivery)
            .filter(NotificationDelivery.job_id == row.job_id, NotificationDelivery.attempt_no >= row.attempt_no)
            .all()
        )
        return not any(_PHASE_RANK.get(other.phase, 0) > _PHASE_RANK.get(row.phase, 0) for other in latest_terminal)

    @staticmethod
    def _job_allows(db, job_id: str, phase: str, attempt_no: int, event_reference: str | None = None) -> bool:
        job = db.query(RecordingJob).filter_by(job_id=job_id).first()
        if job is None or attempt_no != (job.attempt_no or 1):
            return False
        if phase in _STAGE_PHASES:
            if phase == "retry":
                return job.status in {"failed", "queued"}
            return job.status == phase or (phase == "started" and job.status == "starting")
        if phase in {"completed", "uploading"}:
            return not job.youtube_video_id and job.status in {"succeeded", "uploading"}
        if phase == "failed":
            return job.status in {"failed", "canceled"}
        if phase == "uploaded":
            return bool(job.youtube_video_id) and job.youtube_video_id == event_reference
        return False

    def retry_failed(self, delivery_id: int, *, confirmed_absent: bool = False) -> bool:
        """Queue an operator retry; ambiguous sends require explicit receipt checking.

        Return False for a missing, acknowledged, superseded, active, or now-stale
        event. Raise ValueError only when an uncertain send was not confirmed absent.
        The dispatcher uses the normal bounded retry and ordering checks afterward.
        """
        with self._session_factory() as db:
            row = db.get(NotificationDelivery, delivery_id)
            if row is None or row.state not in {"failed", "uncertain"}:
                return False
            if not self._current(db, row):
                row.state = "superseded"
                row.payload_text = ""
                row.next_attempt_at = None
                db.commit()
                return False
            if row.state == "uncertain" and not confirmed_absent:
                raise ValueError("Delivery outcome is uncertain; confirm not received before retrying")
            row.state = "pending"
            row.attempt_count = 0
            row.last_error = None
            row.next_attempt_at = utc_now()
            db.commit()
            return True

    async def retry_due(self) -> None:
        self._recover_unowned_sends()
        with self._session_factory() as db:
            ids = [
                row.id
                for row in db.query(NotificationDelivery)
                .filter(
                    NotificationDelivery.state == "pending",
                    or_(
                        NotificationDelivery.next_attempt_at.is_(None),
                        NotificationDelivery.next_attempt_at <= utc_now(),
                    ),
                )
                .order_by(NotificationDelivery.id)
                .limit(100)
                .all()
            ]
        results = await asyncio.gather(*(self._deliver(delivery_id) for delivery_id in ids), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Telegram outbox retry failed: %s", type(result).__name__)

    async def _deliver(self, delivery_id: int) -> int | None:
        self._inflight[delivery_id] += 1
        try:
            return await self._deliver_one(delivery_id)
        finally:
            self._inflight[delivery_id] -= 1
            if not self._inflight[delivery_id]:
                del self._inflight[delivery_id]

    async def _deliver_one(self, delivery_id: int) -> int | None:
        with self._session_factory() as db:
            row = db.get(NotificationDelivery, delivery_id)
            if row is None:
                return None
            key = (row.job_id, row.chat_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock, self._semaphore:
            with self._session_factory() as db:
                row = db.get(NotificationDelivery, delivery_id)
                if row.state == "acknowledged":
                    return row.message_id
                if row.state != "pending" or (row.next_attempt_at and ensure_utc(row.next_attempt_at) > utc_now()):
                    return None
                if not self._current(db, row):
                    row.state = "superseded"
                    row.payload_text = ""
                    row.next_attempt_at = None
                    db.commit()
                    return None
                previous = (
                    db.query(NotificationDelivery)
                    .filter(
                        NotificationDelivery.job_id == row.job_id,
                        NotificationDelivery.chat_id == row.chat_id,
                        NotificationDelivery.message_id.is_not(None),
                    )
                    .order_by(NotificationDelivery.id.desc())
                    .first()
                )
                row.message_id = previous.message_id if previous else None
                row.operation = "edit" if row.message_id else "send"
                row.state = "sending"
                row.attempt_count += 1
                db.commit()
                chat_id, message_id, text = row.chat_id, row.message_id, row.payload_text

            def before_fallback():
                with self._session_factory() as db:
                    target = db.get(NotificationDelivery, delivery_id)
                    target.operation = "send"
                    target.message_id = None
                    db.commit()

            try:
                result = await self._sender(chat_id, message_id, text, before_fallback)
            except asyncio.CancelledError:
                # Keep sending state: start() handles possible send side effects safely.
                raise
            except Exception as exc:
                logger.error("Telegram delivery dispatcher failed: %s", type(exc).__name__)
                result = DeliveryResult("uncertain", error="dispatcher_exception")
            with self._session_factory() as db:
                row = db.get(NotificationDelivery, delivery_id)
                if result.message_id is not None:
                    row.message_id = result.message_id
                row.last_error = result.error
                if result.state == "acknowledged":
                    row.acknowledged_at = utc_now()
                    row.payload_text = ""
                    row.next_attempt_at = None
                    if row.state != "superseded":
                        row.state = "acknowledged"
                    logger.info(
                        "Telegram API acknowledged job %s type=%s chat=%s message_id=%s",
                        row.job_id,
                        row.notification_type,
                        row.chat_id,
                        row.message_id,
                    )
                elif row.state != "superseded":
                    row.state = result.state
                    row.next_attempt_at = None
                    if result.state == "pending":
                        if row.attempt_count >= _MAX_ATTEMPTS:
                            row.state = "failed"
                            row.last_error = "retry_exhausted:" + (result.error or "unknown")
                        else:
                            delay = max(15 * 2 ** (row.attempt_count - 1), result.retry_after or 0)
                            row.next_attempt_at = utc_now() + timedelta(seconds=min(delay, 86400))
                db.commit()
            return result.message_id if result.state == "acknowledged" else None
