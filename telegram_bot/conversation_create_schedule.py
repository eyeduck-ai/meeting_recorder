"""Telegram conversation for creating schedules."""

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

from database.models import Meeting, ScheduleType
from services.errors import NotFoundError, ValidationError
from services.runtime_config import RuntimeConfigError
from services.schedule_service import ScheduleCreateData, get_schedule_service
from telegram_bot import get_db_session
from telegram_bot.conversation_common import (
    _parse_duration_minutes,
    _parse_time_text,
    _validate_duration_minutes,
    cancel_conversation,
)
from telegram_bot.keyboards import (
    get_confirm_keyboard,
    get_duration_inline_keyboard,
    get_main_menu_keyboard,
    get_meetings_inline_keyboard,
    get_time_inline_keyboard,
    get_youtube_inline_keyboard,
)
from utils.timezone import from_local

logger = logging.getLogger(__name__)


class CreateScheduleStates(IntEnum):
    """States for schedule creation conversation."""

    SELECT_MEETING = auto()
    SELECT_TIME = auto()
    INPUT_CUSTOM_TIME = auto()
    SELECT_DURATION = auto()
    INPUT_CUSTOM_DURATION = auto()
    SELECT_YOUTUBE = auto()
    CONFIRM = auto()


async def create_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the schedule creation wizard."""
    from recording.worker import get_worker
    from scheduling.job_runner import get_job_runner
    from services.job_runtime_state import JobRuntimeStateService

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

        worker = get_worker()
        runner = get_job_runner()
        runtime_snapshot = JobRuntimeStateService().build_snapshot(db, worker=worker, runner=runner)
        recording_warning = ""
        warning_lines = []
        if runtime_snapshot.available_slots <= 0:
            warning_lines.append("⚠️ 錄製容量已滿，選擇「現在」將會排隊等待")
        if runtime_snapshot.queue_length:
            warning_lines.append(f"⏳ 佇列中: {runtime_snapshot.queue_length} 筆")
        if runtime_snapshot.retry_waiting_count:
            warning_lines.append(f"🔁 等待重試: {runtime_snapshot.retry_waiting_count} 筆")
        if warning_lines:
            recording_warning = "\n".join(warning_lines) + "\n\n"

        # Clear any previous wizard data
        context.user_data.clear()

        text = f"📅 新增排程 (1/4)\n\n{recording_warning}請選擇要錄製的會議："
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
        f"📅 新增排程 (2/4)\n\n會議: {context.user_data['meeting_name']}\n\n請選擇開始時間：",
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
            f"📅 新增排程 (2/4)\n\n"
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
        f"📅 新增排程 (3/4)\n\n會議: {context.user_data['meeting_name']}\n時間: {time_display}\n\n請選擇錄製時長：",
        reply_markup=get_duration_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_DURATION


async def input_custom_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle custom time text input."""
    text = update.message.text.strip()
    now = datetime.now()
    start_time = _parse_time_text(text, now=now)

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
        f"📅 新增排程 (3/4)\n\n"
        f"會議: {context.user_data['meeting_name']}\n"
        f"時間: {start_time.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"請選擇錄製時長：",
        reply_markup=get_duration_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_DURATION


def _build_youtube_step_text(context: ContextTypes.DEFAULT_TYPE, duration_min: int) -> str:
    """Build step-4 text after duration is selected."""
    start_time = context.user_data["start_time"]
    is_immediate = context.user_data.get("is_immediate", False)
    if is_immediate:
        time_display = "立即開始"
    else:
        time_display = start_time.strftime("%Y-%m-%d %H:%M")

    return (
        f"📅 新增排程 (4/4)\n\n"
        f"會議: {context.user_data['meeting_name']}\n"
        f"時間: {time_display}\n"
        f"時長: {duration_min} 分鐘\n\n"
        f"是否自動上傳 YouTube？"
    )


async def select_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle duration selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    duration_value = query.data.split(":")[1]
    if duration_value == "custom":
        await query.edit_message_text(
            "📅 新增排程 (3/4)\n\n"
            f"會議: {context.user_data['meeting_name']}\n\n"
            "請輸入錄製時長：\n\n"
            "可用格式範例：\n"
            "• `45`（分鐘）\n"
            "• `1h30m`\n"
            "• `90m`\n"
            "• `2:15`（時:分）\n\n"
            "輸入 /cancel 取消",
            parse_mode="Markdown",
        )
        return CreateScheduleStates.INPUT_CUSTOM_DURATION

    duration_min = int(duration_value)
    error_msg = _validate_duration_minutes(duration_min)
    if error_msg:
        await query.edit_message_text(
            f"❌ {error_msg}\n\n請重新選擇錄製時長：", reply_markup=get_duration_inline_keyboard()
        )
        return CreateScheduleStates.SELECT_DURATION

    context.user_data["duration_min"] = duration_min

    await query.edit_message_text(
        _build_youtube_step_text(context, duration_min),
        reply_markup=get_youtube_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_YOUTUBE


async def input_custom_duration_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle custom duration text input."""
    text = update.message.text.strip()
    duration_min = _parse_duration_minutes(text)

    if duration_min is None:
        await update.message.reply_text(
            "❌ 無法解析時長格式\n\n"
            "請使用以下格式：\n"
            "• `45`（分鐘）\n"
            "• `1h30m`\n"
            "• `90m`\n"
            "• `2:15`（時:分）\n\n"
            "輸入 /cancel 取消",
            parse_mode="Markdown",
        )
        return CreateScheduleStates.INPUT_CUSTOM_DURATION

    error_msg = _validate_duration_minutes(duration_min)
    if error_msg:
        await update.message.reply_text(f"❌ {error_msg}\n\n請重新輸入錄製時長：")
        return CreateScheduleStates.INPUT_CUSTOM_DURATION

    context.user_data["duration_min"] = duration_min
    await update.message.reply_text(
        _build_youtube_step_text(context, duration_min),
        reply_markup=get_youtube_inline_keyboard(),
    )
    return CreateScheduleStates.SELECT_YOUTUBE


async def select_youtube_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle YouTube upload option selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    youtube_value = query.data.split(":")[1]  # "unlisted", "private", or "no"

    if youtube_value == "no":
        context.user_data["youtube_enabled"] = False
        context.user_data["youtube_privacy"] = "unlisted"
    else:
        context.user_data["youtube_enabled"] = True
        context.user_data["youtube_privacy"] = youtube_value

    start_time = context.user_data["start_time"]
    is_immediate = context.user_data.get("is_immediate", False)
    duration_min = context.user_data["duration_min"]
    end_time = start_time + timedelta(minutes=duration_min)
    youtube_text = f"YouTube: {youtube_value}" if context.user_data["youtube_enabled"] else "YouTube: 否"

    if is_immediate:
        summary = (
            f"📋 確認立即錄製\n\n"
            f"會議: {context.user_data['meeting_name']}\n"
            f"開始: 立即\n"
            f"時長: {duration_min} 分鐘\n"
            f"解析度: 1920x1080\n"
            f"{youtube_text}\n\n"
            f"確定要開始錄製嗎？"
        )
    else:
        summary = (
            f"📋 確認排程資訊\n\n"
            f"會議: {context.user_data['meeting_name']}\n"
            f"開始: {start_time.strftime('%Y-%m-%d %H:%M')}\n"
            f"結束: {end_time.strftime('%H:%M')}\n"
            f"時長: {duration_min} 分鐘\n"
            f"解析度: 1920x1080\n"
            f"{youtube_text}\n\n"
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

        schedule_service = get_schedule_service()
        schedule = schedule_service.create_schedule(
            db,
            ScheduleCreateData(
                meeting_id=context.user_data["meeting_id"],
                schedule_type=ScheduleType.ONCE.value,
                start_time=from_local(start_time),
                duration_sec=context.user_data["duration_min"] * 60,
                enabled=True,
                youtube_enabled=context.user_data.get("youtube_enabled", False),
                youtube_privacy=context.user_data.get("youtube_privacy", "unlisted"),
            ),
            sync_scheduler=not is_immediate,
        )

        if is_immediate:
            result = schedule_service.trigger_schedule(db, schedule.id)
            if result and result.accepted and result.status == "queued":
                await query.edit_message_text(
                    f"✅ 排程已建立並加入佇列\n\n"
                    f"會議: {context.user_data['meeting_name']}\n"
                    f"時長: {context.user_data['duration_min']} 分鐘\n"
                    f"排程 ID: {schedule.id}\n"
                    f"佇列位置: {result.queue_position}"
                )
            elif result and result.accepted:
                await query.edit_message_text(
                    f"✅ 已開始錄製！\n\n"
                    f"會議: {context.user_data['meeting_name']}\n"
                    f"時長: {context.user_data['duration_min']} 分鐘\n"
                    f"排程 ID: {schedule.id}\n\n"
                    f"使用 /stop 停止錄製"
                )
            elif result and result.status == "duplicate":
                await query.edit_message_text(f"⚠️ 排程已建立，但此排程已在執行或佇列中\n\n排程 ID: {schedule.id}")
            else:
                await query.edit_message_text(
                    f"⚠️ 排程已建立但啟動延遲\n\n排程 ID: {schedule.id}\n可能有其他錄製進行中，將自動排隊執行"
                )
        else:
            await query.edit_message_text(
                f"✅ 排程建立成功！\n\n"
                f"排程 ID: {schedule.id}\n"
                f"會議: {context.user_data['meeting_name']}\n"
                f"時間: {start_time.strftime('%Y-%m-%d %H:%M')}\n"
                f"時長: {context.user_data['duration_min']} 分鐘"
            )

    except (NotFoundError, ValidationError, RuntimeConfigError) as e:
        logger.error(f"Failed to create schedule: {e}")
        await query.edit_message_text(f"建立排程失敗: {e}")
    except Exception as e:
        logger.error(f"Failed to create schedule: {e}")
        await query.edit_message_text(f"建立排程失敗: {e}")
    finally:
        db.close()

    context.user_data.clear()
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
                CallbackQueryHandler(select_duration_callback, pattern=r"^(duration:(?:\d+|custom)|cancel)$"),
            ],
            CreateScheduleStates.INPUT_CUSTOM_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_custom_duration_handler),
            ],
            CreateScheduleStates.SELECT_YOUTUBE: [
                CallbackQueryHandler(select_youtube_callback, pattern=r"^(youtube:\w+|cancel)$"),
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
