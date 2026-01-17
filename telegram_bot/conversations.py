"""Telegram conversation handlers for schedule creation wizard."""

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

from database.models import Meeting, Schedule, ScheduleType
from telegram_bot import get_db_session
from telegram_bot.keyboards import (
    get_confirm_keyboard,
    get_duration_inline_keyboard,
    get_main_menu_keyboard,
    get_meetings_inline_keyboard,
    get_time_inline_keyboard,
)
from utils.timezone import from_local

logger = logging.getLogger(__name__)


class CreateScheduleStates(IntEnum):
    """States for schedule creation conversation."""

    SELECT_MEETING = auto()
    SELECT_TIME = auto()
    INPUT_CUSTOM_TIME = auto()
    SELECT_DURATION = auto()
    CONFIRM = auto()


async def create_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the schedule creation wizard."""
    from recording.worker import get_worker

    db = get_db_session()
    try:
        meetings = db.query(Meeting).order_by(Meeting.name).all()

        if not meetings:
            text = "尚無會議設定\n請先在 Web UI 建立會議"
            if update.callback_query:
                await update.callback_query.edit_message_text(text)
            else:
                await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
            return ConversationHandler.END

        # Check if recording is in progress
        worker = get_worker()
        recording_warning = ""
        if worker.is_busy:
            recording_warning = "⚠️ 目前有錄製進行中\n選擇「現在」將會排隊等待\n\n"

        # Clear any previous wizard data
        context.user_data.clear()

        text = f"📅 新增排程 (1/3)\n\n{recording_warning}請選擇要錄製的會議："
        keyboard = get_meetings_inline_keyboard(meetings)

        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)

        return CreateScheduleStates.SELECT_MEETING
    finally:
        db.close()


async def select_meeting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle meeting selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消新增排程")
        return ConversationHandler.END

    meeting_id = int(query.data.split(":")[1])
    context.user_data["meeting_id"] = meeting_id

    db = get_db_session()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        context.user_data["meeting_name"] = meeting.name if meeting else "Unknown"
    finally:
        db.close()

    await query.edit_message_text(
        f"📅 新增排程 (2/3)\n\n會議: {context.user_data['meeting_name']}\n\n請選擇開始時間：",
        reply_markup=get_time_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_TIME


async def select_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle time selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    time_value = query.data.split(":")[1]

    # Handle custom time input
    if time_value == "custom":
        await query.edit_message_text(
            f"📅 新增排程 (2/3)\n\n"
            f"會議: {context.user_data['meeting_name']}\n\n"
            f"請輸入開始時間：\n\n"
            f"格式範例：\n"
            f"• `01/15 14:30` (今年)\n"
            f"• `2024/01/15 14:30`\n"
            f"• `14:30` (今天)\n\n"
            f"輸入 /cancel 取消",
            parse_mode="Markdown",
        )
        return CreateScheduleStates.INPUT_CUSTOM_TIME

    # Handle preset time options
    if time_value == "now":
        start_time = datetime.now().replace(second=0, microsecond=0)
        context.user_data["is_immediate"] = True
    else:
        offset_minutes = int(time_value)
        start_time = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=offset_minutes)
        context.user_data["is_immediate"] = False

    context.user_data["start_time"] = start_time

    # Show different text for immediate vs scheduled
    if context.user_data.get("is_immediate"):
        time_display = "立即開始"
    else:
        time_display = start_time.strftime("%Y-%m-%d %H:%M")

    await query.edit_message_text(
        f"📅 新增排程 (3/3)\n\n會議: {context.user_data['meeting_name']}\n時間: {time_display}\n\n請選擇錄製時長：",
        reply_markup=get_duration_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_DURATION


async def input_custom_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle custom time text input."""
    text = update.message.text.strip()

    # Try to parse the time in various formats
    now = datetime.now()
    start_time = None

    formats_to_try = [
        "%Y/%m/%d %H:%M",  # 2024/01/15 14:30
        "%Y-%m-%d %H:%M",  # 2024-01-15 14:30
        "%m/%d %H:%M",  # 01/15 14:30 (current year)
        "%m-%d %H:%M",  # 01-15 14:30 (current year)
        "%d %H:%M",  # 15 14:30 (current month/year)
        "%H:%M",  # 14:30 (today)
    ]

    for fmt in formats_to_try:
        try:
            parsed = datetime.strptime(text, fmt)
            # Fill in missing year/month/day
            if fmt == "%H:%M":
                start_time = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                # If time has passed today, use tomorrow
                if start_time < now:
                    start_time += timedelta(days=1)
            elif fmt in ["%m/%d %H:%M", "%m-%d %H:%M"]:
                start_time = parsed.replace(year=now.year, second=0, microsecond=0)
                # If date has passed this year, use next year
                if start_time < now:
                    start_time = start_time.replace(year=now.year + 1)
            elif fmt == "%d %H:%M":
                start_time = parsed.replace(year=now.year, month=now.month, second=0, microsecond=0)
                # If day has passed this month, use next month
                if start_time < now:
                    if now.month == 12:
                        start_time = start_time.replace(year=now.year + 1, month=1)
                    else:
                        start_time = start_time.replace(month=now.month + 1)
            else:
                start_time = parsed.replace(second=0, microsecond=0)
            break
        except ValueError:
            continue

    if not start_time:
        await update.message.reply_text(
            "❌ 無法解析時間格式\n\n"
            "請使用以下格式：\n"
            "• `01/15 14:30` (今年)\n"
            "• `2024/01/15 14:30`\n"
            "• `14:30` (今天)\n\n"
            "輸入 /cancel 取消",
            parse_mode="Markdown",
        )
        return CreateScheduleStates.INPUT_CUSTOM_TIME

    if start_time < now:
        await update.message.reply_text(
            "❌ 時間不能是過去\n\n請輸入未來的時間：",
        )
        return CreateScheduleStates.INPUT_CUSTOM_TIME

    context.user_data["start_time"] = start_time

    await update.message.reply_text(
        f"📅 新增排程 (3/3)\n\n"
        f"會議: {context.user_data['meeting_name']}\n"
        f"時間: {start_time.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"請選擇錄製時長：",
        reply_markup=get_duration_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_DURATION


async def select_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    duration_min = int(query.data.split(":")[1])
    context.user_data["duration_min"] = duration_min

    start_time = context.user_data["start_time"]
    is_immediate = context.user_data.get("is_immediate", False)
    end_time = start_time + timedelta(minutes=duration_min)

    if is_immediate:
        summary = (
            f"📋 確認立即錄製\n\n"
            f"會議: {context.user_data['meeting_name']}\n"
            f"開始: 立即\n"
            f"時長: {duration_min} 分鐘\n"
            f"解析度: 1920x1080\n\n"
            f"確定要開始錄製嗎？"
        )
    else:
        summary = (
            f"📋 確認排程資訊\n\n"
            f"會議: {context.user_data['meeting_name']}\n"
            f"開始: {start_time.strftime('%Y-%m-%d %H:%M')}\n"
            f"結束: {end_time.strftime('%H:%M')}\n"
            f"時長: {duration_min} 分鐘\n"
            f"解析度: 1920x1080\n\n"
            f"確定要建立嗎？"
        )

    await query.edit_message_text(summary, reply_markup=get_confirm_keyboard())
    return CreateScheduleStates.CONFIRM


async def confirm_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle schedule confirmation."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    # Create the schedule
    db = get_db_session()
    try:
        is_immediate = context.user_data.get("is_immediate", False)
        start_time = context.user_data["start_time"]

        schedule = Schedule(
            meeting_id=context.user_data["meeting_id"],
            schedule_type=ScheduleType.ONCE.value,
            start_time=from_local(start_time),
            duration_sec=context.user_data["duration_min"] * 60,
            resolution_w=1920,
            resolution_h=1080,
            enabled=True,
        )
        db.add(schedule)
        db.commit()
        db.refresh(schedule)

        # Add to scheduler and trigger if immediate
        try:
            from scheduling.scheduler import get_scheduler

            scheduler = get_scheduler()

            if is_immediate:
                # Trigger immediately
                job_id = await scheduler.trigger_schedule(schedule.id)
                if job_id:
                    await query.edit_message_text(
                        f"✅ 已開始錄製！\n\n"
                        f"會議: {context.user_data['meeting_name']}\n"
                        f"時長: {context.user_data['duration_min']} 分鐘\n"
                        f"Job: {job_id[:8]}...\n\n"
                        f"使用 /stop 停止錄製"
                    )
                else:
                    await query.edit_message_text(
                        f"⚠️ 排程已建立但啟動延遲\n\n排程 ID: {schedule.id}\n可能有其他錄製進行中，將自動排隊執行"
                    )
            else:
                # Just add to scheduler for future execution
                if scheduler.is_running:
                    scheduler.add_schedule(schedule)
                await query.edit_message_text(
                    f"✅ 排程建立成功！\n\n"
                    f"排程 ID: {schedule.id}\n"
                    f"會議: {context.user_data['meeting_name']}\n"
                    f"時間: {start_time.strftime('%Y-%m-%d %H:%M')}\n"
                    f"時長: {context.user_data['duration_min']} 分鐘"
                )
        except Exception as e:
            logger.warning(f"Could not add schedule to scheduler: {e}")
            await query.edit_message_text(f"✅ 排程已儲存\n\n排程 ID: {schedule.id}\n⚠️ 排程器狀態異常: {e}")

    except Exception as e:
        logger.error(f"Failed to create schedule: {e}")
        await query.edit_message_text(f"建立排程失敗: {e}")
    finally:
        db.close()

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("已取消操作")
    else:
        await update.message.reply_text("已取消操作", reply_markup=get_main_menu_keyboard())
    return ConversationHandler.END


def get_create_schedule_conversation() -> ConversationHandler:
    """Get the schedule creation ConversationHandler.

    This handler is used for both scheduled recordings and immediate recordings.
    Entry points: /record command or "➕ 新增排程" button.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("record", create_schedule_start),
            MessageHandler(filters.Regex("^➕ 新增排程$"), create_schedule_start),
        ],
        states={
            CreateScheduleStates.SELECT_MEETING: [
                CallbackQueryHandler(select_meeting_callback, pattern=r"^(select_meeting:\d+|cancel)$"),
            ],
            CreateScheduleStates.SELECT_TIME: [
                CallbackQueryHandler(select_time_callback, pattern=r"^(time:\w+|cancel)$"),
            ],
            CreateScheduleStates.INPUT_CUSTOM_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_custom_time_handler),
            ],
            CreateScheduleStates.SELECT_DURATION: [
                CallbackQueryHandler(select_duration_callback, pattern=r"^(duration:\d+|cancel)$"),
            ],
            CreateScheduleStates.CONFIRM: [
                CallbackQueryHandler(confirm_schedule_callback, pattern=r"^(confirm:\w+|cancel)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel$"),
        ],
        per_message=False,
    )
