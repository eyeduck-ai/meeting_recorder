"""Telegram notification functions."""

import logging

from database.models import RecordingJob, TelegramUser, get_session_local
from telegram_bot.bot import get_bot

logger = logging.getLogger(__name__)

# Map notification types to TelegramUser filter attributes
_NOTIFICATION_FILTERS = {
    "start": "notify_on_start",
    "complete": "notify_on_complete",
    "failure": "notify_on_failure",
    "upload": "notify_on_upload",
}

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
    "RECORDING_START_FAILED": "錄製啟動失敗",
    "RECORDING_INTERRUPTED": "錄製中斷",
    "FFMPEG_ERROR": "FFmpeg 錯誤",
    "BROWSER_CRASHED": "瀏覽器當機",
    "VIRTUAL_ENV_ERROR": "虛擬環境錯誤",
    "DISK_FULL": "磁碟空間不足",
    "CANCELED": "已取消",
    "INTERNAL_ERROR": "內部錯誤",
}


async def send_to_approved_users(message: str, notification_type: str = "all") -> None:
    """Send a message to all approved users based on their notification preferences.

    Args:
        message: The message to send
        notification_type: One of 'start', 'complete', 'failure', 'upload', or 'all'
    """
    bot = await get_bot()
    if bot is None:
        return

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        query = db.query(TelegramUser).filter(TelegramUser.approved == True)

        # Filter by notification preference if specified
        filter_attr = _NOTIFICATION_FILTERS.get(notification_type)
        if filter_attr:
            query = query.filter(getattr(TelegramUser, filter_attr) == True)

        users = query.all()

        for user in users:
            try:
                await bot.send_message(chat_id=user.chat_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send message to {user.display_name}: {e}")
    finally:
        db.close()


async def notify_recording_started(job: RecordingJob) -> None:
    """Notify users that a recording has started."""
    message = f"🔴 開始錄製\n\n會議: {job.meeting_code}\n名稱: {job.display_name}\n時長: {job.duration_sec // 60} 分鐘"
    await send_to_approved_users(message, "start")
    logger.info(f"Sent recording start notification for job {job.job_id}")


async def notify_recording_completed(job: RecordingJob) -> None:
    """Notify users that a recording has completed successfully."""
    duration_str = f"{job.duration_actual_sec / 60:.1f}" if job.duration_actual_sec else "-"
    file_size_str = f"{job.file_size / 1024 / 1024:.1f} MB" if job.file_size else "-"

    message = f"✅ 錄製完成\n\n會議: {job.meeting_code}\n時長: {duration_str} 分鐘\n大小: {file_size_str}"

    if job.youtube_enabled and not job.youtube_video_id:
        message += "\n\n上傳 YouTube 中..."

    await send_to_approved_users(message, "complete")
    logger.info(f"Sent recording complete notification for job {job.job_id}")


async def notify_recording_failed(job: RecordingJob) -> None:
    """Notify users that a recording has failed."""
    # Build detailed error information
    error_info = ""
    if job.error_code:
        error_code = job.error_code.value if hasattr(job.error_code, "value") else str(job.error_code)
        desc = _ERROR_DESCRIPTIONS.get(error_code, error_code)
        error_info = f"\n原因: {desc}"

    if job.error_message:
        error_info += f"\n詳情: {job.error_message[:100]}"

    # Check for diagnostic data availability
    diagnostic_hint = ""
    if job.has_screenshot or job.has_html_dump or job.has_console_log:
        diagnostic_hint = "\n\n📎 已收集診斷資料，可在 Web UI 查看"

    status_value = job.status.value if hasattr(job.status, "value") else str(job.status)
    message = f"❌ 錄製失敗\n\n會議: {job.meeting_code}\n狀態: {status_value}{error_info}{diagnostic_hint}"
    await send_to_approved_users(message, "failure")
    logger.info(f"Sent recording failure notification for job {job.job_id}")


async def notify_youtube_upload_completed(job: RecordingJob, video_url: str) -> None:
    """Notify users that a YouTube upload has completed."""
    message = f"📺 YouTube 上傳完成\n\n會議: {job.meeting_code}\n連結: {video_url}"
    await send_to_approved_users(message, "upload")
    logger.info(f"Sent YouTube upload notification for job {job.job_id}")


async def send_to_user(chat_id: int, message: str):
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
