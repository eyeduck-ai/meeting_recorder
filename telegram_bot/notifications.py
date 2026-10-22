"""Telegram notification functions with single-message updates."""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from telegram.error import BadRequest, Forbidden, RetryAfter

from config.settings import get_settings
from database.models import JobStatus, RecordingJob, Schedule, TelegramUser
from database.session import get_session_local
from providers import get_provider_metadata
from services.notification_delivery import DeliveryResult, NotificationDeliveryService
from telegram_bot.bot import get_bot

logger = logging.getLogger(__name__)

TELEGRAM_NOTIFICATION_TIMEOUT_SEC = 10.0
TELEGRAM_NOTIFICATION_FANOUT_CONCURRENCY = 3

# Map error codes to concise user-facing English descriptions.
_ERROR_DESCRIPTIONS = {
    "JOIN_TIMEOUT": "Timed out while joining the meeting",
    "JOIN_FAILED": "Failed to join meeting",
    "INVALID_URL": "Invalid meeting link",
    "MEETING_NOT_FOUND": "Meeting was not found",
    "PASSWORD_REQUIRED": "Meeting password is required",
    "PASSWORD_INCORRECT": "Meeting password is incorrect",
    "LOBBY_TIMEOUT": "Timed out waiting in the lobby",
    "LOBBY_REJECTED": "Rejected from the lobby by the host",
    "NEVER_JOINED": "Never joined the meeting",
    "RECORDING_START_FAILED": "Failed to start recording",
    "RECORDING_INTERRUPTED": "Recording was interrupted",
    "FFMPEG_ERROR": "FFmpeg failed",
    "BROWSER_CRASHED": "Browser crashed",
    "VIRTUAL_ENV_ERROR": "Virtual recording environment failed",
    "DISK_FULL": "Disk space is full",
    "CANCELED": "Recording was canceled",
    "INTERNAL_ERROR": "Internal error",
    "NETWORK_ERROR": "Network error",
}

_GENERIC_DISPLAY_LABELS = {
    "recorder bot",
}

_PHASE_LABELS = {
    "started": "🔄 Starting",
    "starting": "🔄 Starting",
    "joining": "🚪 Joining meeting",
    "waiting_lobby": "⏳ Waiting in lobby",
    "recording": "🔴 Recording",
    "finalizing": "💾 Finalizing",
    "completed": "✅ Recording completed",
    "failed": "❌ Recording failed",
    "uploading": "⏳ Uploading",
    "uploaded": "📺 Uploaded",
}

_STATUS_TO_PHASE = {
    JobStatus.STARTING.value: "starting",
    JobStatus.JOINING.value: "joining",
    JobStatus.WAITING_LOBBY.value: "waiting_lobby",
    JobStatus.RECORDING.value: "recording",
    JobStatus.FINALIZING.value: "finalizing",
    JobStatus.SUCCEEDED.value: "completed",
    JobStatus.FAILED.value: "failed",
    JobStatus.CANCELED.value: "failed",
    JobStatus.UPLOADING.value: "uploading",
}


def _normalize_phase(phase_or_status: str | JobStatus) -> str:
    """Normalize a phase/status value to message phase."""
    value = phase_or_status.value if hasattr(phase_or_status, "value") else str(phase_or_status)
    return _STATUS_TO_PHASE.get(value, value)


def _format_provider(provider: str | None) -> str:
    """Format provider for user-facing Telegram messages."""
    if not provider:
        return "Unknown"
    normalized = str(provider).strip()
    if not normalized:
        return "Unknown"
    try:
        return get_provider_metadata(normalized).label
    except ValueError:
        return normalized.replace("_", " ").title()


def _format_datetime(dt: datetime | None) -> str:
    """Format datetime to local date/time string.

    Note: DB stores naive UTC datetimes. This function converts them to local time.
    """
    if not dt:
        return "-"
    try:
        settings = get_settings()
        tz = ZoneInfo(settings.timezone)
        utc_dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def _shorten_text(text: str, limit: int = 60) -> str:
    """Trim text for concise Telegram messages."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _get_error_reason(job: RecordingJob) -> str:
    """Get concise error reason for failed messages."""
    if job.error_code:
        error_code = job.error_code.value if hasattr(job.error_code, "value") else str(job.error_code)
        return _ERROR_DESCRIPTIONS.get(error_code, error_code)

    if job.error_message:
        return _shorten_text(str(job.error_message), limit=50)

    return "Unknown error"


def _valid_user_label(value: object) -> str | None:
    """Return a safe user-facing label or None when the source is unusable."""
    if value is None:
        return None
    label = str(value).strip()
    if not label or "�" in label:
        return None
    return label


def _valid_display_fallback_label(value: object) -> str | None:
    """Return a display-name label only when it is specific enough for a meeting title."""
    label = _valid_user_label(value)
    if not label:
        return None
    normalized = " ".join(label.casefold().split())
    if normalized in _GENERIC_DISPLAY_LABELS:
        return None
    return label


def _meeting_name_from_loaded_schedule(job: RecordingJob) -> str | None:
    """Read a meeting name from an already attached schedule relationship."""
    try:
        schedule = getattr(job, "schedule", None)
        meeting = getattr(schedule, "meeting", None) if schedule is not None else None
        name = getattr(meeting, "name", None) if meeting is not None else None
    except Exception as exc:
        logger.debug("Could not read loaded meeting relationship for job %s: %s", getattr(job, "job_id", None), exc)
        return None
    return _valid_user_label(name)


def _resolve_meeting_label(job: RecordingJob) -> str:
    """Resolve the user-facing meeting label for a recording job."""
    loaded_name = _meeting_name_from_loaded_schedule(job)
    if loaded_name:
        return loaded_name

    schedule_id = getattr(job, "schedule_id", None)
    if schedule_id:
        SessionLocal = get_session_local()
        db = SessionLocal()
        try:
            schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
            name = getattr(getattr(schedule, "meeting", None), "name", None) if schedule is not None else None
            label = _valid_user_label(name)
            if label:
                return label
        except Exception as exc:
            logger.warning(
                "Could not resolve meeting name for Telegram notification job %s: %s",
                getattr(job, "job_id", None),
                exc,
            )
        finally:
            db.close()

    return (
        _valid_display_fallback_label(getattr(job, "display_name", None))
        or _valid_user_label(getattr(job, "meeting_code", None))
        or "Unknown meeting"
    )


def _build_status_message(
    job: RecordingJob,
    phase: str = "started",
    video_url: str | None = None,
    meeting_label: str | None = None,
) -> str:
    """Build a unified status message for a recording job.

    Args:
        job: The recording job
        phase: Status phase for display
        video_url: YouTube video URL (for uploaded phase)
        meeting_label: Resolved meeting display label
    """
    phase = _normalize_phase(phase)
    resolved_meeting_label = _valid_user_label(meeting_label) or _resolve_meeting_label(job)

    lines = [
        f"🎬 Meeting: {resolved_meeting_label}",
        f"Provider: {_format_provider(getattr(job, 'provider', None))}",
        f"Status: {_PHASE_LABELS.get(phase, phase)}",
    ]

    # Timeline
    if job.started_at:
        lines.append(f"Started: {_format_datetime(job.started_at)}")
    if phase in ("completed", "failed", "uploading", "uploaded") and job.completed_at:
        lines.append(f"Ended: {_format_datetime(job.completed_at)}")

    # Recording info (for completed/uploaded)
    if phase in ("completed", "uploading", "uploaded"):
        if job.duration_actual_sec:
            lines.append(f"Duration: {job.duration_actual_sec / 60:.1f} minutes")

    # Error info (for failed)
    if phase == "failed":
        lines.append(f"Error: {_get_error_reason(job)}")
        if job.has_screenshot or job.has_html_dump:
            lines.append("Diagnostics: Web UI")

    # YouTube section
    if job.youtube_enabled:
        if phase == "uploading":
            lines.append("YouTube: Uploading")
        elif phase == "uploaded" and video_url:
            lines.append(f"YouTube: {video_url}")
        elif phase == "completed":
            lines.append("YouTube: Pending upload")

    return "\n".join(lines)


async def _get_approved_chat_ids(notification_type: str) -> list[int]:
    """Get list of approved chat IDs based on notification preferences."""
    # Map notification types to TelegramUser filter attributes
    filter_attrs = {
        "start": "notify_on_start",
        "complete": "notify_on_complete",
        "failure": "notify_on_failure",
        "upload": "notify_on_upload",
    }

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        query = db.query(TelegramUser).filter(TelegramUser.approved == True)
        filter_attr = filter_attrs.get(notification_type)
        if filter_attr:
            query = query.filter(getattr(TelegramUser, filter_attr) == True)
        users = query.all()
        return [user.chat_id for user in users]
    finally:
        db.close()


async def _telegram_call_with_timeout(
    call,
    *args,
    operation: str,
    chat_id: int,
    **kwargs,
) -> tuple[bool, object | None]:
    """Run one Telegram API call with a bounded timeout."""
    try:
        awaitable = call(*args, chat_id=chat_id, **kwargs)
        result = await asyncio.wait_for(awaitable, timeout=TELEGRAM_NOTIFICATION_TIMEOUT_SEC)
        return True, result
    except BadRequest as e:
        if operation == "edit" and "message is not modified" in str(e).lower():
            logger.debug("Telegram edit for chat %s was already up to date", chat_id)
            return True, None
        logger.error("Telegram %s failed for chat %s: %s", operation, chat_id, type(e).__name__)
    except TimeoutError:
        logger.warning(
            "Telegram %s timed out for chat %s after %.1fs",
            operation,
            chat_id,
            TELEGRAM_NOTIFICATION_TIMEOUT_SEC,
        )
    except Exception as e:
        logger.error("Telegram %s failed for chat %s: %s", operation, chat_id, type(e).__name__)
    return False, None


_delivery_service: NotificationDeliveryService | None = None


def get_notification_delivery_service() -> NotificationDeliveryService:
    """Shared dispatcher; lifecycle is owned by the application's lifespan."""
    global _delivery_service
    if _delivery_service is None:
        _delivery_service = NotificationDeliveryService(
            lambda: get_session_local()(),
            _deliver_one_chat,
            concurrency=TELEGRAM_NOTIFICATION_FANOUT_CONCURRENCY,
        )
    return _delivery_service


async def _deliver_one_chat(chat_id: int, message_id: int | None, message: str, before_fallback) -> DeliveryResult:
    bot = await get_bot()
    if bot is None:
        return DeliveryResult("pending", error="bot_unavailable")
    operation = "edit" if message_id else "send"
    while True:
        try:
            if message_id:
                result = await asyncio.wait_for(
                    bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message),
                    timeout=TELEGRAM_NOTIFICATION_TIMEOUT_SEC,
                )
                return DeliveryResult("acknowledged", message_id=message_id)
            result = await asyncio.wait_for(
                bot.send_message(chat_id=chat_id, text=message),
                timeout=TELEGRAM_NOTIFICATION_TIMEOUT_SEC,
            )
            returned_id = getattr(result, "message_id", None)
            if not isinstance(returned_id, int):
                return DeliveryResult("uncertain", error="missing_message_id")
            return DeliveryResult("acknowledged", message_id=returned_id)
        except BadRequest as exc:
            reason = str(exc).lower()
            if message_id and "message is not modified" in reason:
                return DeliveryResult("acknowledged", message_id=message_id)
            if message_id and any(
                value in reason
                for value in (
                    "message to edit not found",
                    "message can't be edited",
                    "message cannot be edited",
                )
            ):
                before_fallback()
                message_id = None
                operation = "fallback-send"
                continue
            logger.warning("Telegram %s rejected for chat %s: bad_request", operation, chat_id)
            return DeliveryResult("failed", error="bad_request")
        except Forbidden:
            logger.warning("Telegram %s rejected for chat %s: forbidden", operation, chat_id)
            return DeliveryResult("failed", error="forbidden")
        except RetryAfter as exc:
            retry_after = exc.retry_after
            delay = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)
            return DeliveryResult("pending", error="rate_limited", retry_after=delay)
        except TimeoutError:
            logger.warning("Telegram %s timed out for chat %s", operation, chat_id)
            return DeliveryResult("pending" if message_id else "uncertain", error="timeout")
        except Exception:
            # Do not persist exception text: transport errors may contain request URLs/tokens.
            logger.warning("Telegram %s failed for chat %s: transport_error", operation, chat_id)
            return DeliveryResult("pending" if message_id else "uncertain", error="transport_error")


async def _send_or_edit_status_message(
    *,
    job: RecordingJob,
    message: str,
    notification_type: str,
    phase: str,
    attempt_no: int | None = None,
    event_reference: str | None = None,
) -> int | None:
    """Persist a per-job, per-chat event before bounded best-effort delivery."""
    try:
        chat_ids = await _get_approved_chat_ids(notification_type)
        return await get_notification_delivery_service().enqueue(
            job_id=job.job_id,
            phase=phase,
            notification_type=notification_type,
            attempt_no=attempt_no or getattr(job, "attempt_no", None) or 1,
            message=message,
            chat_ids=chat_ids,
            event_reference=event_reference,
        )
    except Exception as exc:
        # Avoid SQL/transport exception text that can contain payloads or tokens.
        logger.error(
            "Could not persist or dispatch Telegram notification for job %s: %s", job.job_id, type(exc).__name__
        )
        return None


async def notify_recording_status(job: RecordingJob, status: str | JobStatus) -> int | None:
    """Send or update stage notification for active recording statuses."""
    phase = _normalize_phase(status)
    meeting_label = _resolve_meeting_label(job)
    message = _build_status_message(job, phase, meeting_label=meeting_label)
    message_id = await _send_or_edit_status_message(
        job=job,
        message=message,
        notification_type="start",
        phase=phase,
    )
    if message_id is not None:
        logger.info(f"Updated recording stage notification for job {job.job_id}: {phase}")
    return message_id


async def notify_recording_completed(job: RecordingJob) -> int | None:
    """Update notification to show recording completed."""
    phase = "uploading" if job.youtube_enabled else "completed"

    meeting_label = _resolve_meeting_label(job)
    message = _build_status_message(job, phase, meeting_label=meeting_label)
    message_id = await _send_or_edit_status_message(job=job, message=message, notification_type="complete", phase=phase)
    if message_id is not None:
        logger.info(f"Updated recording complete notification for job {job.job_id}")
    return message_id


async def notify_recording_failed(job: RecordingJob) -> int | None:
    """Update notification to show recording failed."""
    meeting_label = _resolve_meeting_label(job)
    message = _build_status_message(job, "failed", meeting_label=meeting_label)
    message_id = await _send_or_edit_status_message(
        job=job, message=message, notification_type="failure", phase="failed"
    )
    if message_id is not None:
        logger.info(f"Updated recording failure notification for job {job.job_id}")
    return message_id


async def notify_recording_retry(
    job: RecordingJob,
    attempt: int,
    next_retry_sec: int,
    error_message: str,
) -> int | None:
    """Send notification when recording retry is attempted.

    Args:
        job: The recording job
        attempt: Current retry attempt number
        next_retry_sec: Seconds until next retry
        error_message: Error message that triggered the retry

    Returns:
        Message ID if sent successfully, None otherwise
    """
    meeting_label = _resolve_meeting_label(job)
    lines = [
        f"🔄 Meeting: {meeting_label}",
        f"Provider: {_format_provider(getattr(job, 'provider', None))}",
        f"Status: Retrying attempt {attempt}",
        f"Retry in: {next_retry_sec} seconds",
        f"Reason: {_shorten_text(error_message, limit=50)}",
    ]
    message = "\n".join(lines)
    first_message_id = await _send_or_edit_status_message(
        job=job,
        message=message,
        notification_type="failure",
        phase="retry",
        attempt_no=attempt,
    )

    if first_message_id is not None:
        logger.info(f"Sent recording retry notification for job {job.job_id} (attempt {attempt})")
    return first_message_id


async def notify_youtube_upload_completed(job: RecordingJob, video_url: str) -> int | None:
    """Update notification to show YouTube upload completed."""
    meeting_label = _resolve_meeting_label(job)
    message = _build_status_message(job, "uploaded", video_url, meeting_label=meeting_label)
    message_id = await _send_or_edit_status_message(
        job=job, message=message, notification_type="upload", phase="uploaded", event_reference=job.youtube_video_id
    )
    if message_id is not None:
        logger.info(f"Updated YouTube upload notification for job {job.job_id}")
    return message_id


async def send_to_user(chat_id: int, message: str) -> bool:
    """Send a message to a specific user."""
    bot = await get_bot()
    if bot is None:
        return False

    sent, _ = await _telegram_call_with_timeout(
        bot.send_message,
        chat_id=chat_id,
        text=message,
        operation="send",
    )
    return sent
