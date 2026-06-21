"""Telegram conversation for editing schedules."""

import logging
from datetime import datetime, timedelta
from enum import IntEnum, auto

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config.settings import get_settings
from database.models import Schedule
from services.schedule_service import get_schedule_service
from telegram_bot.conversation_common import _parse_time_text, cancel_conversation
from telegram_bot.keyboards import (
    get_delete_confirm_keyboard,
    get_edit_confirm_keyboard,
    get_edit_time_keyboard,
    get_main_menu_keyboard,
    get_schedules_select_keyboard,
)
from telegram_bot.session import get_db_session
from utils.timezone import from_local, to_local

logger = logging.getLogger(__name__)


class EditScheduleStates(IntEnum):
    """States for schedule editing conversation."""

    SELECT_SCHEDULE = auto()
    SELECT_TIME = auto()
    INPUT_CUSTOM_TIME = auto()
    CONFIRM = auto()
    CONFIRM_DELETE = auto()


async def edit_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the schedule editing wizard."""
    db = get_db_session()
    try:
        schedules = (
            db.query(Schedule)
            .filter(Schedule.enabled == True, Schedule.next_run_at != None)
            .order_by(Schedule.next_run_at)
            .limit(10)
            .all()
        )

        if not schedules:
            text = "目前無可編輯的排程"
            if update.callback_query:
                await update.callback_query.edit_message_text(text)
            else:
                await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
            return ConversationHandler.END

        context.user_data.clear()

        settings = get_settings()
        tz = settings.timezone
        keyboard = get_schedules_select_keyboard(schedules, tz)

        text = "✏️ 編輯排程時間\n\n請選擇要編輯的排程："
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)

        return EditScheduleStates.SELECT_SCHEDULE
    finally:
        db.close()


async def edit_select_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle schedule selection for editing."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    schedule_id = int(query.data.split(":")[1])
    context.user_data["edit_schedule_id"] = schedule_id

    db = get_db_session()
    try:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            await query.edit_message_text("排程不存在")
            return ConversationHandler.END

        context.user_data["meeting_name"] = schedule.meeting.name

        settings = get_settings()
        local_time = to_local(schedule.next_run_at, settings.timezone) if schedule.next_run_at else None
        current_time_str = local_time.strftime("%Y-%m-%d %H:%M") if local_time else "-"

        await query.edit_message_text(
            f"✏️ 編輯排程時間\n\n會議: {schedule.meeting.name}\n目前時間: {current_time_str}\n\n請選擇新的開始時間：",
            reply_markup=get_edit_time_keyboard(),
        )
        return EditScheduleStates.SELECT_TIME
    finally:
        db.close()


async def edit_select_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle new time selection for editing."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    time_value = query.data.split(":")[1]

    if time_value == "custom":
        await query.edit_message_text(
            f"✏️ 編輯排程時間\n\n"
            f"會議: {context.user_data['meeting_name']}\n\n"
            f"請輸入新的開始時間：\n\n"
            f"格式範例：\n"
            f"• `01/15 14:30` (今年)\n"
            f"• `2024/01/15 14:30`\n"
            f"• `14:30` (今天)\n\n"
            f"輸入 /cancel 取消",
            parse_mode="Markdown",
        )
        return EditScheduleStates.INPUT_CUSTOM_TIME

    if time_value == "delete":
        await query.edit_message_text(
            f"🗑️ 刪除排程\n\n會議: {context.user_data['meeting_name']}\n\n❗ 確定要刪除此排程嗎？",
            reply_markup=get_delete_confirm_keyboard(),
        )
        return EditScheduleStates.CONFIRM_DELETE

    offset_minutes = int(time_value)
    new_time = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=offset_minutes)
    context.user_data["new_start_time"] = new_time

    await query.edit_message_text(
        f"✏️ 確認修改\n\n"
        f"會議: {context.user_data['meeting_name']}\n"
        f"新時間: {new_time.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"確定要修改嗎？",
        reply_markup=get_edit_confirm_keyboard(),
    )
    return EditScheduleStates.CONFIRM


async def edit_input_custom_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle custom time text input for editing."""
    text = update.message.text.strip()
    now = datetime.now()

    new_time = _parse_time_text(text)

    if not new_time:
        await update.message.reply_text(
            "❌ 無法解析時間格式\n\n"
            "請使用以下格式：\n"
            "• `01/15 14:30` (今年)\n"
            "• `2024/01/15 14:30`\n"
            "• `14:30` (今天)\n\n"
            "輸入 /cancel 取消",
            parse_mode="Markdown",
        )
        return EditScheduleStates.INPUT_CUSTOM_TIME

    if new_time < now:
        await update.message.reply_text("❌ 時間不能是過去\n\n請輸入未來的時間：")
        return EditScheduleStates.INPUT_CUSTOM_TIME

    context.user_data["new_start_time"] = new_time

    await update.message.reply_text(
        f"✏️ 確認修改\n\n"
        f"會議: {context.user_data['meeting_name']}\n"
        f"新時間: {new_time.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"確定要修改嗎？",
        reply_markup=get_edit_confirm_keyboard(),
    )
    return EditScheduleStates.CONFIRM


async def edit_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle edit confirmation - update database and scheduler."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    schedule_id = context.user_data["edit_schedule_id"]
    new_time = context.user_data["new_start_time"]

    db = get_db_session()
    try:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            await query.edit_message_text("排程不存在")
            return ConversationHandler.END

        get_schedule_service().update_schedule(db, schedule_id, {"start_time": from_local(new_time)})

        await query.edit_message_text(
            f"✅ 排程已更新\n\n會議: {context.user_data['meeting_name']}\n新時間: {new_time.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception as e:
        logger.error(f"Failed to update schedule: {e}")
        await query.edit_message_text(f"更新排程失敗: {e}")
    finally:
        db.close()

    context.user_data.clear()
    return ConversationHandler.END


async def edit_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle delete confirmation - remove schedule from database and scheduler."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    schedule_id = context.user_data["edit_schedule_id"]
    meeting_name = context.user_data["meeting_name"]

    db = get_db_session()
    try:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            await query.edit_message_text("排程不存在")
            return ConversationHandler.END

        get_schedule_service().delete_schedule(db, schedule_id)

        await query.edit_message_text(f"🗑️ 排程已刪除\n\n會議: {meeting_name}")
    except Exception as e:
        logger.error(f"Failed to delete schedule: {e}")
        await query.edit_message_text(f"刪除排程失敗: {e}")
    finally:
        db.close()

    context.user_data.clear()
    return ConversationHandler.END


def get_edit_schedule_conversation() -> ConversationHandler:
    """Get the schedule editing ConversationHandler.

    Entry points: /edit command or "✏️ 編輯排程" button.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_schedule_start),
            MessageHandler(filters.Regex("^✏️ 編輯排程$"), edit_schedule_start),
        ],
        states={
            EditScheduleStates.SELECT_SCHEDULE: [
                CallbackQueryHandler(edit_select_schedule_callback, pattern=r"^(edit_schedule:\d+|cancel)$"),
            ],
            EditScheduleStates.SELECT_TIME: [
                CallbackQueryHandler(edit_select_time_callback, pattern=r"^(edit_time:\w+|cancel)$"),
            ],
            EditScheduleStates.INPUT_CUSTOM_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_input_custom_time_handler),
            ],
            EditScheduleStates.CONFIRM: [
                CallbackQueryHandler(edit_confirm_callback, pattern=r"^(edit_confirm:\w+|cancel)$"),
            ],
            EditScheduleStates.CONFIRM_DELETE: [
                CallbackQueryHandler(edit_delete_confirm_callback, pattern=r"^(edit_confirm:\w+|cancel)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel$"),
        ],
        per_message=False,
    )
