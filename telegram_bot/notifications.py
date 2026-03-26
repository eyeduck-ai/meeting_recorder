"""Telegram notification functions with single-message updates."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import get_settings
from database.models import JobStatus, RecordingJob, TelegramUser, get_session_local
from telegram_bot.bot import get_bot

logger = logging.getLogger(__name__)

# Map error codes to user-friendly descriptions (in Chinese)
_ERROR_DESCRIPTIONS = {
    "JOIN_TIMEOUT": "無法在時限內加入會議",
    "JOIN_FAILED": "加入會議失敗",
    "INVALID_URL": "無效的會議連結",
    "MEETING_NOT_FOUND": "會議不存在",
    "PASSWORD_REQUIRED": "需要密碼",
    "PASSWORD_INCORRECT": "密碼錯誤",
    "LOBBY_TIMEOUT": "等候室等待逾時 (未被准入)",
    "LOBBY_REJECTED": "被主持人拒絕進入",
    "NEVER_JOINED": "始終未能加入會議",
    "RECORDING_START_FAILED": "錄製啟動失敗",
    "RECORDING_INTERRUPTED": "錄製中斷",
    "FFMPEG_ERROR": "FFmpeg 錯誤",
    "BROWSER_CRASHED": "瀏覽器當機",
    "VIRTUAL_ENV_ERROR": "虛擬環境錯誤",
    "DISK_FULL": "磁碟空間不足",
    "CANCELED": "已取消",
    "INTERNAL_ERROR": "內部錯誤",
    "NETWORK_ERROR": "網路連線錯誤",
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


def _format_time(dt: datetime | None) -> str:
    """Format datetime to local time string.

    Note: DB stores naive UTC datetimes. This function converts them to local time.
    """
    if not dt:
        return "-"
    try:
        settings = get_settings()
        tz = ZoneInfo(settings.timezone)
        # Strip any tzinfo (treat as UTC), then convert to local
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        local_dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        return local_dt.strftime("%H:%M")
    except Exception:
        return dt.strftime("%H:%M") if dt else "-"


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

    return "未知錯誤"


def _build_status_message(
    job: RecordingJob,
    phase: str = "started",
    video_url: str | None = None,
) -> str:
    """Build a unified status message for a recording job.

    Args:
        job: The recording job
        phase: Status phase for display
        video_url: YouTube video URL (for uploaded phase)
    """
    phase = _normalize_phase(phase)

    lines = [f"🎬 {job.meeting_code}"]

    # Status line
    status_icons = {
        "started": "🔄 啟動中",
        "starting": "🔄 啟動中",
        "joining": "🚪 加入會議中",
        "waiting_lobby": "⏳ 等候室等待中",
        "recording": "🔴 錄製中",
        "finalizing": "💾 收尾中",
        "completed": "✅ 錄製完成",
        "failed": "❌ 錄製失敗",
        "uploading": "⏳ 上傳中",
        "uploaded": "📺 已上傳",
    }
    lines.append(f"狀態：{status_icons.get(phase, phase)}")

    # Timeline
    if job.started_at:
        lines.append(f"開始：{_format_time(job.started_at)}")
    if phase in ("completed", "failed", "uploading", "uploaded") and job.completed_at:
        lines.append(f"結束：{_format_time(job.completed_at)}")

    # Recording info (for completed/uploaded)
    if phase in ("completed", "uploading", "uploaded"):
        if job.duration_actual_sec:
            lines.append(f"時長：{job.duration_actual_sec / 60:.1f} 分")

    # Error info (for failed)
    if phase == "failed":
        lines.append(f"原因：{_get_error_reason(job)}")
        if job.has_screenshot or job.has_html_dump:
            lines.append("診斷：Web UI")

    # YouTube section
    if job.youtube_enabled:
        if phase == "uploading":
            lines.append("YouTube：上傳中")
        elif phase == "uploaded" and video_url:
            lines.append(f"YouTube：{video_url}")
        elif phase == "completed":
            lines.append("YouTube：等待上傳")

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


async def _send_or_edit_status_message(
    *,
    job: RecordingJob,
    message: str,
    notification_type: str,
) -> int | None:
    """Send a new message or edit existing Telegram message."""
    bot = await get_bot()
    if bot is None:
        return None

    chat_ids = await _get_approved_chat_ids(notification_type)
    first_message_id = job.telegram_message_id

    for chat_id in chat_ids:
        try:
            if job.telegram_message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=job.telegram_message_id,
                    text=message,
                )
                if first_message_id is None:
                    first_message_id = job.telegram_message_id
            else:
                sent = await bot.send_message(chat_id=chat_id, text=message)
                if first_message_id is None:
                    first_message_id = sent.message_id
        except Exception as e:
            logger.error(f"Failed to send/edit notification to {chat_id}: {e}")
            if job.telegram_message_id:
                try:
                    sent = await bot.send_message(chat_id=chat_id, text=message)
                    if first_message_id is None:
                        first_message_id = sent.message_id
                except Exception as send_err:
                    logger.error(f"Failed to fallback-send notification to {chat_id}: {send_err}")

    return first_message_id


async def notify_recording_status(job: RecordingJob, status: str | JobStatus) -> int | None:
    """Send or update stage notification for active recording statuses."""
    phase = _normalize_phase(status)
    message = _build_status_message(job, phase)
    message_id = await _send_or_edit_status_message(
        job=job,
        message=message,
        notification_type="start",
    )
    logger.info(f"Updated recording stage notification for job {job.job_id}: {phase}")
    return message_id


async def notify_recording_started(job: RecordingJob) -> int | None:
    """Backward-compatible wrapper for recording-stage notification."""
    return await notify_recording_status(job, JobStatus.RECORDING)


async def notify_recording_completed(job: RecordingJob) -> None:
    """Update notification to show recording completed."""
    phase = "uploading" if job.youtube_enabled else "completed"

    message = _build_status_message(job, phase)
    await _send_or_edit_status_message(job=job, message=message, notification_type="start")

    logger.info(f"Updated recording complete notification for job {job.job_id}")


async def notify_recording_failed(job: RecordingJob) -> None:
    """Update notification to show recording failed."""
    message = _build_status_message(job, "failed")
    await _send_or_edit_status_message(job=job, message=message, notification_type="start")

    logger.info(f"Updated recording failure notification for job {job.job_id}")


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
    # Build retry notification message
    lines = [
        f"🔄 {job.meeting_code}",
        f"狀態：重試第 {attempt} 次",
        f"{next_retry_sec} 秒後重試",
        f"原因：{_shorten_text(error_message, limit=50)}",
    ]
    message = "\n".join(lines)
    first_message_id = await _send_or_edit_status_message(
        job=job,
        message=message,
        notification_type="failure",
    )

    logger.info(f"Sent recording retry notification for job {job.job_id} (attempt {attempt})")
    return first_message_id


async def notify_youtube_upload_completed(job: RecordingJob, video_url: str) -> None:
    """Update notification to show YouTube upload completed."""
    message = _build_status_message(job, "uploaded", video_url)
    await _send_or_edit_status_message(job=job, message=message, notification_type="start")

    logger.info(f"Updated YouTube upload notification for job {job.job_id}")


# Legacy functions for backward compatibility
async def send_to_approved_users(message: str, notification_type: str = "all") -> None:
    """Send a message to all approved users (legacy)."""
    bot = await get_bot()
    if bot is None:
        return

    chat_ids = await _get_approved_chat_ids(notification_type)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")


async def send_to_user(chat_id: int, message: str) -> bool:
    """Send a message to a specific user."""
    bot = await get_bot()
    if bot is None:
        return False

    try:
        await bot.send_message(chat_id=chat_id, text=message)
        return True
    except Exception as e:
        logger.error(f"Failed to send message to chat {chat_id}: {e}")
        return False
