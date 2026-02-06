"""Telegram notification functions with single-message updates."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import get_settings
from database.models import RecordingJob, TelegramUser, get_session_local
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
        return local_dt.strftime("%H:%M:%S")
    except Exception:
        return dt.strftime("%H:%M:%S") if dt else "-"


def _build_status_message(
    job: RecordingJob,
    phase: str = "started",
    video_url: str | None = None,
) -> str:
    """Build a unified status message for a recording job.

    Args:
        job: The recording job
        phase: One of 'started', 'completed', 'failed', 'uploading', 'uploaded'
        video_url: YouTube video URL (for uploaded phase)
    """
    # Header
    lines = [f"🎬 錄製任務 | {job.meeting_code}", ""]

    # Status line
    status_icons = {
        "started": "🔴 錄製中",
        "completed": "✅ 錄製完成",
        "failed": "❌ 錄製失敗",
        "uploading": "⏳ 上傳中",
        "uploaded": "📺 已上傳",
    }
    lines.append(f"📋 狀態：{status_icons.get(phase, phase)}")
    lines.append("━━━━━━━━━━━━━━━━━")

    # Timeline
    if job.started_at:
        lines.append(f"⏱ 開始：{_format_time(job.started_at)}")
    if phase in ("completed", "failed", "uploading", "uploaded") and job.completed_at:
        lines.append(f"⏱ 結束：{_format_time(job.completed_at)}")

    # Recording info (for completed/uploaded)
    if phase in ("completed", "uploading", "uploaded"):
        if job.duration_actual_sec:
            lines.append(f"⏱ 時長：{job.duration_actual_sec / 60:.1f} 分鐘")
        if job.file_size:
            lines.append(f"📦 大小：{job.file_size / 1024 / 1024:.1f} MB")

    # Error info (for failed)
    if phase == "failed":
        if job.error_code:
            error_code = job.error_code.value if hasattr(job.error_code, "value") else str(job.error_code)
            desc = _ERROR_DESCRIPTIONS.get(error_code, error_code)
            lines.append(f"❌ 原因：{desc}")
        if job.has_screenshot or job.has_html_dump:
            lines.append("")
            lines.append("📎 診斷資料可在 Web UI 查看")

    # YouTube section
    if job.youtube_enabled:
        lines.append("━━━━━━━━━━━━━━━━━")
        if phase == "uploading":
            lines.append("📺 YouTube：上傳中...")
        elif phase == "uploaded" and video_url:
            lines.append(f"📺 YouTube：{video_url}")
        elif phase == "completed":
            lines.append("📺 YouTube：等待上傳")

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


async def notify_recording_started(job: RecordingJob) -> int | None:
    """Send initial recording notification and return message_id for updates."""
    bot = await get_bot()
    if bot is None:
        return None

    message = _build_status_message(job, "started")
    chat_ids = await _get_approved_chat_ids("start")

    first_message_id = None
    for chat_id in chat_ids:
        try:
            sent = await bot.send_message(chat_id=chat_id, text=message)
            if first_message_id is None:
                first_message_id = sent.message_id
        except Exception as e:
            logger.error(f"Failed to send start notification to {chat_id}: {e}")

    logger.info(f"Sent recording start notification for job {job.job_id}")
    return first_message_id


async def notify_recording_completed(job: RecordingJob) -> None:
    """Update notification to show recording completed."""
    bot = await get_bot()
    if bot is None:
        return

    phase = "completed" if job.youtube_enabled else "completed"
    if job.youtube_enabled:
        phase = "uploading"

    message = _build_status_message(job, phase)
    chat_ids = await _get_approved_chat_ids("complete")

    for chat_id in chat_ids:
        try:
            if job.telegram_message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=job.telegram_message_id,
                    text=message,
                )
            else:
                await bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to update completion notification to {chat_id}: {e}")

    logger.info(f"Updated recording complete notification for job {job.job_id}")


async def notify_recording_failed(job: RecordingJob) -> None:
    """Update notification to show recording failed."""
    bot = await get_bot()
    if bot is None:
        return

    message = _build_status_message(job, "failed")
    chat_ids = await _get_approved_chat_ids("failure")

    for chat_id in chat_ids:
        try:
            if job.telegram_message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=job.telegram_message_id,
                    text=message,
                )
            else:
                await bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to update failure notification to {chat_id}: {e}")

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
    bot = await get_bot()
    if bot is None:
        return None

    # Build retry notification message
    lines = [
        f"🔄 錄製重試 | {job.meeting_code}",
        "",
        f"📋 狀態：第 {attempt} 次重試",
        "━━━━━━━━━━━━━━━━━",
        "⚠️ 錯誤：網路連線問題",
        f"⏱ 將於 {next_retry_sec} 秒後重試",
        "",
        f"📝 詳細：{error_message[:100]}..." if len(error_message) > 100 else f"📝 詳細：{error_message}",
    ]
    message = "\n".join(lines)

    chat_ids = await _get_approved_chat_ids("failure")  # Use failure notification preference

    first_message_id = None
    for chat_id in chat_ids:
        try:
            if job.telegram_message_id:
                # Update existing message
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=job.telegram_message_id,
                    text=message,
                )
                if first_message_id is None:
                    first_message_id = job.telegram_message_id
            else:
                # Send new message
                sent = await bot.send_message(chat_id=chat_id, text=message)
                if first_message_id is None:
                    first_message_id = sent.message_id
        except Exception as e:
            logger.error(f"Failed to send retry notification to {chat_id}: {e}")

    logger.info(f"Sent recording retry notification for job {job.job_id} (attempt {attempt})")
    return first_message_id


async def notify_youtube_upload_completed(job: RecordingJob, video_url: str) -> None:
    """Update notification to show YouTube upload completed."""
    bot = await get_bot()
    if bot is None:
        return

    message = _build_status_message(job, "uploaded", video_url)
    chat_ids = await _get_approved_chat_ids("upload")

    for chat_id in chat_ids:
        try:
            if job.telegram_message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=job.telegram_message_id,
                    text=message,
                )
            else:
                await bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to update YouTube notification to {chat_id}: {e}")

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
