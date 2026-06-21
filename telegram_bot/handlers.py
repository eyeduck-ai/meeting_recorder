"""Telegram command handlers."""

import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config.settings import get_settings
from database.models import (
    JobStatus,
    Meeting,
    RecordingJob,
    Schedule,
    TelegramUser,
)
from services.errors import NotFoundError
from services.schedule_service import get_schedule_service
from telegram_bot.conversation_create_meeting import get_create_meeting_conversation
from telegram_bot.conversation_create_schedule import get_create_schedule_conversation
from telegram_bot.conversation_edit_schedule import get_edit_schedule_conversation
from telegram_bot.keyboards import get_main_menu_keyboard, get_meetings_list_keyboard
from telegram_bot.session import get_db_session
from utils.timezone import ensure_utc, to_local, utc_now

logger = logging.getLogger(__name__)

# Map job status to display text
_JOB_STATUS_MAP = {
    "starting": "🔄 啟動中",
    "joining": "🚪 加入會議中",
    "waiting_lobby": "⏳ 等候室等待中",
    "recording": "🔴 錄製中",
    "finalizing": "💾 處理中",
}


def _is_schedule_visible(schedule: Schedule) -> bool:
    """Return True when schedule is upcoming or currently in progress."""
    now = utc_now()

    if schedule.next_run_at and ensure_utc(schedule.next_run_at) > now:
        return True

    schedule_type = (
        schedule.schedule_type.value if hasattr(schedule.schedule_type, "value") else str(schedule.schedule_type)
    )
    if schedule_type != "once" or not schedule.start_time:
        return False

    start_time = ensure_utc(schedule.start_time)
    end_time = start_time + timedelta(seconds=schedule.duration_sec)
    return end_time > now


def _get_visible_schedules(db, limit: int = 5) -> list[Schedule]:
    """Get non-expired schedules for Telegram list views."""
    schedules = (
        db.query(Schedule)
        .filter(Schedule.enabled == True)
        .order_by(Schedule.next_run_at.asc().nullslast(), Schedule.start_time.asc().nullslast(), Schedule.id.asc())
        .limit(50)
        .all()
    )
    visible = [s for s in schedules if _is_schedule_visible(s)]
    return visible[:limit]


def _format_schedule_list(schedules: list[Schedule]) -> str:
    """Format a list of schedules for display.

    Args:
        schedules: List of Schedule objects to format

    Returns:
        Formatted string for display
    """
    if not schedules:
        return "無即將執行的排程"

    settings = get_settings()
    tz = settings.timezone

    lines = ["📋 即將執行的排程\n"]
    for s in schedules:
        schedule_type = s.schedule_type.value if hasattr(s.schedule_type, "value") else str(s.schedule_type)
        now = utc_now()

        reference_time = s.next_run_at
        in_progress = False

        if schedule_type == "once" and s.start_time:
            start_utc = ensure_utc(s.start_time)
            end_utc = start_utc + timedelta(seconds=s.duration_sec)
            if start_utc <= now < end_utc:
                reference_time = s.start_time
                in_progress = True
            elif reference_time is None:
                reference_time = s.start_time

        local_start = to_local(reference_time, tz) if reference_time else None
        start = local_start.strftime("%m/%d %H:%M") if local_start else "-"
        duration_min = s.duration_sec // 60
        end_time = ""
        if local_start:
            local_end = local_start + timedelta(seconds=s.duration_sec)
            end_time = f" ~ {local_end.strftime('%H:%M')}"

        schedule_type_str = schedule_type.upper()
        progress_tag = "（進行中）" if in_progress else ""
        lines.append(f"• {s.meeting.name} [{schedule_type_str}]{progress_tag}\n  {start}{end_time} ({duration_min}分)")
    return "\n".join(lines)


def get_or_create_user(
    db, chat_id: int, username: str | None, first_name: str | None, last_name: str | None
) -> TelegramUser:
    """Get or create a Telegram user."""
    user = db.query(TelegramUser).filter(TelegramUser.chat_id == chat_id).first()
    if not user:
        user = TelegramUser(
            chat_id=chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update user info if changed
        if user.username != username or user.first_name != first_name or user.last_name != last_name:
            user.username = username
            user.first_name = first_name
            user.last_name = last_name
            user.last_interaction_at = utc_now()
            db.commit()
    return user


def require_approved(func):
    """Decorator to require user approval for commands."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        db = get_db_session()
        try:
            chat = update.effective_chat
            user_data = update.effective_user

            # Handle callback queries
            if update.callback_query:
                chat = update.callback_query.message.chat

            user = get_or_create_user(
                db,
                chat.id,
                user_data.username if user_data else None,
                user_data.first_name if user_data else None,
                user_data.last_name if user_data else None,
            )

            if not user.approved:
                text = "帳號待審核中\n請聯繫管理員核准"
                if update.callback_query:
                    await update.callback_query.answer(text, show_alert=True)
                else:
                    await update.message.reply_text(text)
                return

            # Update last interaction
            user.last_interaction_at = utc_now()
            db.commit()

            return await func(update, context)
        finally:
            db.close()

    return wrapper


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - register user and show welcome message with Reply Keyboard."""
    db = get_db_session()
    try:
        chat = update.effective_chat
        user_data = update.effective_user
        user = get_or_create_user(
            db,
            chat.id,
            user_data.username if user_data else None,
            user_data.first_name if user_data else None,
            user_data.last_name if user_data else None,
        )

        if user.approved:
            await update.message.reply_text(
                f"歡迎回來 {user.display_name}！\n\n"
                "請使用下方選單操作，或輸入指令：\n"
                "/list - 查看排程\n"
                "/record - 新增排程/立即錄製\n"
                "/edit - 編輯排程時間\n"
                "/help - 說明",
                reply_markup=get_main_menu_keyboard(),
            )
        else:
            await update.message.reply_text(
                f"歡迎使用 Meeting Recorder！\n\n用戶 ID：{chat.id}\n\n帳號待審核中，請等待管理員核准。"
            )
            logger.info(f"New Telegram user registered: {user.display_name} (chat_id={chat.id})")
    finally:
        db.close()


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "Meeting Recorder Bot\n\n"
        "📋 選單按鈕：\n"
        "• 查看排程 - 顯示排程與錄製狀態\n"
        "• 新增排程 - 建立排程或立即錄製\n\n"
        "📝 指令列表：\n"
        "/start - 顯示選單\n"
        "/list - 查看排程\n"
        "/record - 新增排程\n"
        "/edit - 編輯/刪除排程\n"
        "/meetings - 查看/新增會議\n"
        "/stop [job_id] - 停止錄製\n"
        "/help - 顯示說明\n\n"
        "進階設定請使用 Web UI",
        reply_markup=get_main_menu_keyboard(),
    )


@require_approved
async def list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command - list next 5 upcoming schedules with recording status."""
    from recording.worker import get_worker
    from scheduling.job_runner import get_job_runner
    from services.job_runtime_state import JobRuntimeStateService

    db = get_db_session()
    try:
        worker = get_worker()
        runner = get_job_runner()
        runtime_snapshot = JobRuntimeStateService().build_snapshot(
            db,
            worker=worker,
            runner=runner,
            active_jobs_limit=5,
        )
        recording_status = ""
        queue_status = ""

        if runtime_snapshot.active_jobs:
            status_lines = []
            settings = get_settings()
            for current_job in runtime_snapshot.active_jobs:
                status_value = (
                    current_job.status.value if hasattr(current_job.status, "value") else str(current_job.status)
                )
                status_text = _JOB_STATUS_MAP.get(status_value, status_value)
                local_started = to_local(current_job.started_at, settings.timezone) if current_job.started_at else None
                started = local_started.strftime("%H:%M") if local_started else "-"
                status_lines.append(
                    f"• {status_text}\n"
                    f"  ID: {current_job.job_id}\n"
                    f"  會議: {current_job.meeting_code}\n"
                    f"  開始: {started}"
                )
            recording_status = f"🎬 錄製中 ({len(runtime_snapshot.active_jobs)})\n" + "\n".join(status_lines)
            recording_status += f"\n\n{'─' * 20}\n\n"

        queue_length = runtime_snapshot.queue_length
        retry_waiting_count = runtime_snapshot.retry_waiting_count
        if queue_length or retry_waiting_count:
            queue_lines = []
            if queue_length:
                queue_lines.append(f"⏳ 佇列中: {queue_length} 筆")
            if retry_waiting_count:
                queue_lines.append(f"🔁 等待重試: {retry_waiting_count} 筆")
            queue_status = "\n".join(queue_lines) + f"\n\n{'─' * 20}\n\n"

        schedules = _get_visible_schedules(db, limit=5)

        if not schedules and not recording_status and not queue_status:
            await update.message.reply_text("無即將執行的排程", reply_markup=get_main_menu_keyboard())
            return

        lines = []
        if recording_status:
            lines.append(recording_status)
        if queue_status:
            lines.append(queue_status)

        if schedules:
            lines.append(_format_schedule_list(schedules))

        await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu_keyboard())
    finally:
        db.close()


@require_approved
async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command - stop current recording."""
    from recording.worker import get_worker
    from scheduling.job_runner import get_job_runner
    from services.errors import NotFoundError, ServiceError, ValidationError
    from services.job_actions import JobActionService
    from services.job_runtime_state import JobRuntimeStateService

    worker = get_worker()
    runner = get_job_runner()
    db = get_db_session()

    try:
        runtime_snapshot = JobRuntimeStateService().build_snapshot(db, worker=worker, runner=runner)
        requested_job_id = context.args[0].strip() if getattr(context, "args", None) else None
        job = None
        if requested_job_id:
            matches = (
                db.query(RecordingJob)
                .filter(RecordingJob.job_id.startswith(requested_job_id))
                .order_by(RecordingJob.created_at.desc())
                .limit(2)
                .all()
            )
            if len(matches) > 1:
                await update.message.reply_text(
                    "找到多個符合的 job，請輸入完整 job id", reply_markup=get_main_menu_keyboard()
                )
                return
            job = matches[0] if matches else None
        else:
            job = runtime_snapshot.latest_active_job

        if not job:
            await update.message.reply_text("目前無符合的錄製中 job", reply_markup=get_main_menu_keyboard())
            return

        try:
            result = JobActionService(worker=worker, job_runner=runner).stop_job(db, job.job_id)
            if "canceled" in result.message.lower():
                text = f"✅ 已取消 job\nJob: {job.job_id}"
            else:
                text = f"✅ 已發送停止指令\nJob: {job.job_id}\n錄製將於稍後停止"
            await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
        except NotFoundError:
            await update.message.reply_text("找不到指定 job", reply_markup=get_main_menu_keyboard())
        except ValidationError as e:
            status_value = job.status.value if hasattr(job.status, "value") else job.status
            if status_value == JobStatus.UPLOADING.value:
                text = "指定 job 正在處理或上傳中，無法停止"
            elif status_value in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELED.value}:
                text = f"指定 job 已結束，狀態為 {status_value}"
            elif status_value == JobStatus.QUEUED.value:
                text = f"指定 job 已排隊，但目前無法取消: {e}"
            else:
                text = f"無法停止錄製: {e}"
            await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
        except ServiceError as e:
            await update.message.reply_text(f"停止失敗: {e}", reply_markup=get_main_menu_keyboard())
    except Exception as e:
        logger.error(f"Failed to stop recording: {e}")
        await update.message.reply_text(f"停止失敗: {e}", reply_markup=get_main_menu_keyboard())
    finally:
        db.close()


@require_approved
async def meetings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /meetings command - list all meetings with add button."""
    db = get_db_session()
    try:
        meetings = db.query(Meeting).order_by(Meeting.name).all()

        text = "📝 會議列表\n\n"
        if meetings:
            for m in meetings:
                provider = m.provider.upper() if hasattr(m.provider, "upper") else str(m.provider).upper()
                text += f"• {m.name} ({provider})\n"
        else:
            text += "尚無會議設定\n"

        text += "\n點擊下方按鈕新增會議"

        await update.message.reply_text(
            text,
            reply_markup=get_meetings_list_keyboard(meetings),
        )
    finally:
        db.close()


@require_approved
async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Reply Keyboard button presses."""
    text = update.message.text

    if text == "📋 查看排程":
        await list_handler(update, context)
    # Note: "➕ 新增排程" is handled by ConversationHandler


@require_approved
async def schedule_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle schedule action inline buttons (trigger, toggle)."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action = parts[0]

    if action == "back_to_list":
        # Redirect to list handler by editing message
        db = get_db_session()
        try:
            schedules = _get_visible_schedules(db, limit=5)
            await query.edit_message_text(_format_schedule_list(schedules))
        finally:
            db.close()
        return

    schedule_id = int(parts[1])

    db = get_db_session()
    try:
        schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
        if not schedule:
            await query.edit_message_text("排程不存在")
            return

        if action == "trigger":
            result = get_schedule_service().trigger_schedule(db, schedule_id)
            if result and result.accepted and result.status == "queued":
                await query.edit_message_text(
                    f"✅ 已加入佇列 #{schedule_id}\n會議: {schedule.meeting.name}\n佇列位置: {result.queue_position}"
                )
            elif result and result.accepted:
                await query.edit_message_text(f"✅ 已觸發排程 #{schedule_id}\n會議: {schedule.meeting.name}")
            elif result and result.status == "duplicate":
                await query.edit_message_text("此排程已在執行或佇列中")
            else:
                await query.edit_message_text("觸發失敗，可能有其他錄製進行中")

        elif action == "toggle":
            schedule = get_schedule_service().toggle_enabled(db, schedule_id)
            if schedule.enabled:
                await query.edit_message_text(f"✅ 排程 #{schedule_id} 已啟用")
            else:
                await query.edit_message_text(f"⏸️ 排程 #{schedule_id} 已停用")
    except NotFoundError:
        await query.edit_message_text("排程不存在")
    except Exception as e:
        logger.error(f"Schedule action error: {e}")
        await query.edit_message_text(f"操作失敗: {e}")
    finally:
        db.close()


def setup_handlers(application: Application):
    """Setup all command handlers."""
    # Conversation handlers (must be added first for priority)
    application.add_handler(get_create_schedule_conversation())
    application.add_handler(get_edit_schedule_conversation())
    application.add_handler(get_create_meeting_conversation())

    # Command handlers
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("list", list_handler))
    application.add_handler(CommandHandler("meetings", meetings_handler))
    application.add_handler(CommandHandler("stop", stop_handler))
    application.add_handler(CommandHandler("record", lambda u, c: None))  # Handled by conversation
    application.add_handler(CommandHandler("cancel", lambda u, c: None))  # Handled by conversation

    # Reply Keyboard message handler (for menu buttons that aren't in conversation)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("^📋 查看排程$"), menu_button_handler))

    # Inline button callback handlers
    application.add_handler(
        CallbackQueryHandler(schedule_action_callback, pattern=r"^(trigger|toggle|back_to_list)(:\d+)?$")
    )

    logger.info("Telegram handlers configured with keyboards")
