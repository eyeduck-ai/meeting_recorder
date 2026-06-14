"""Shared Telegram conversation helpers."""

import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from config.settings import get_settings
from telegram_bot.keyboards import get_main_menu_keyboard


def _parse_duration_minutes(text: str) -> int | None:
    """Parse user-provided duration text and return total minutes."""
    normalized = text.strip().lower()
    if not normalized:
        return None

    if normalized.isdigit():
        return int(normalized)

    match = re.fullmatch(r"(\d+)\s*:\s*(\d{1,2})", normalized)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        return hours * 60 + minutes

    match = re.fullmatch(r"(\d+)\s*(?:m|min|mins|minute|minutes|分鐘|分)", normalized)
    if match:
        return int(match.group(1))

    match = re.fullmatch(r"(\d+)\s*(?:h|hr|hrs|hour|hours|小時)", normalized)
    if match:
        return int(match.group(1)) * 60

    match = re.fullmatch(
        r"(\d+)\s*(?:h|hr|hrs|hour|hours|小時)\s*(\d+)\s*(?:m|min|mins|minute|minutes|分鐘|分)?",
        normalized,
    )
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))

    return None


def _validate_duration_minutes(duration_min: int) -> str | None:
    """Validate duration against project constraints."""
    if duration_min <= 0:
        return "時長必須大於 0 分鐘"

    settings = get_settings()
    max_duration_min = max(1, settings.max_recording_sec // 60)
    if duration_min > max_duration_min:
        return f"時長不能超過 {max_duration_min} 分鐘"

    return None


def _parse_time_text(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse user-provided time text into a naive local datetime."""
    now = now or datetime.now()

    formats_to_try = [
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%m/%d %H:%M",
        "%m-%d %H:%M",
        "%d %H:%M",
        "%H:%M",
    ]

    for fmt in formats_to_try:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                result = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                if result < now:
                    result += timedelta(days=1)
            elif fmt in ["%m/%d %H:%M", "%m-%d %H:%M"]:
                result = parsed.replace(year=now.year, second=0, microsecond=0)
                if result < now:
                    result = result.replace(year=now.year + 1)
            elif fmt == "%d %H:%M":
                result = parsed.replace(year=now.year, month=now.month, second=0, microsecond=0)
                if result < now:
                    if now.month == 12:
                        result = result.replace(year=now.year + 1, month=1)
                    else:
                        result = result.replace(month=now.month + 1)
            else:
                result = parsed.replace(second=0, microsecond=0)
            return result
        except ValueError:
            continue

    return None


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("已取消操作")
    else:
        await update.message.reply_text("已取消操作", reply_markup=get_main_menu_keyboard())
    return ConversationHandler.END
