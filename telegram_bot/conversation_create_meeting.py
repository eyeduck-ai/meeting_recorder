"""Telegram conversation for creating meetings."""

import logging
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

from providers import get_provider_metadata
from services.meeting_service import MeetingCreateData, get_meeting_service
from telegram_bot.conversation_common import cancel_conversation
from telegram_bot.keyboards import get_meeting_confirm_keyboard, get_provider_keyboard
from telegram_bot.session import get_db_session

logger = logging.getLogger(__name__)


class CreateMeetingStates(IntEnum):
    """States for meeting creation conversation."""

    SELECT_PROVIDER = auto()
    INPUT_NAME = auto()
    INPUT_URL = auto()
    CONFIRM = auto()


async def create_meeting_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the meeting creation wizard."""
    context.user_data.clear()

    text = "📝 新增會議 (1/3)\n\n請選擇會議類型："
    keyboard = get_provider_keyboard()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)

    return CreateMeetingStates.SELECT_PROVIDER


async def select_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle provider selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    try:
        provider_metadata = get_provider_metadata(query.data.split(":")[1])
    except ValueError:
        await query.edit_message_text("不支援的會議類型")
        return ConversationHandler.END

    provider = provider_metadata.name
    context.user_data["provider"] = provider

    await query.edit_message_text(
        f"📝 新增會議 (2/3)\n\n類型: {provider_metadata.label}\n\n請輸入會議名稱：\n\n輸入 /cancel 取消",
    )
    return CreateMeetingStates.INPUT_NAME


async def input_meeting_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle meeting name input."""
    name = update.message.text.strip()

    if len(name) < 1 or len(name) > 100:
        await update.message.reply_text("❌ 名稱長度需在 1-100 字元之間\n\n請重新輸入：")
        return CreateMeetingStates.INPUT_NAME

    context.user_data["name"] = name

    provider = context.user_data["provider"]

    provider_metadata = get_provider_metadata(provider)

    await update.message.reply_text(
        f"📝 新增會議 (3/3)\n\n"
        f"類型: {provider_metadata.label}\n"
        f"名稱: {name}\n\n"
        f"請輸入會議 URL：\n"
        f"{provider_metadata.telegram_url_hint}\n\n"
        f"輸入 /cancel 取消",
    )
    return CreateMeetingStates.INPUT_URL


async def input_meeting_url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle meeting URL input."""
    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text("❌ URL 需以 http:// 或 https:// 開頭\n\n請重新輸入：")
        return CreateMeetingStates.INPUT_URL

    context.user_data["url"] = url

    provider = context.user_data["provider"]
    name = context.user_data["name"]

    await update.message.reply_text(
        f"📋 確認新增會議\n\n類型: {provider.upper()}\n名稱: {name}\nURL: {url}\n\n確定要新增嗎？",
        reply_markup=get_meeting_confirm_keyboard(),
    )
    return CreateMeetingStates.CONFIRM


async def confirm_meeting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle meeting creation confirmation."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("已取消操作")
        return ConversationHandler.END

    db = get_db_session()
    try:
        meeting = get_meeting_service().create_meeting(
            db,
            MeetingCreateData(
                name=context.user_data["name"],
                provider=context.user_data["provider"],
                meeting_code=context.user_data["url"],
                join_url=context.user_data["url"],
            ),
        )

        await query.edit_message_text(
            f"✅ 會議已新增\n\nID: {meeting.id}\n名稱: {meeting.name}\n類型: {meeting.provider.upper()}"
        )
    except Exception as e:
        logger.error(f"Failed to create meeting: {e}")
        await query.edit_message_text(f"新增會議失敗: {e}")
    finally:
        db.close()

    context.user_data.clear()
    return ConversationHandler.END


def get_create_meeting_conversation() -> ConversationHandler:
    """Get the meeting creation ConversationHandler.

    Entry point: "add_meeting" callback from meetings list.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(create_meeting_start, pattern=r"^add_meeting$"),
        ],
        states={
            CreateMeetingStates.SELECT_PROVIDER: [
                CallbackQueryHandler(select_provider_callback, pattern=r"^(provider:\w+|cancel)$"),
            ],
            CreateMeetingStates.INPUT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_meeting_name_handler),
            ],
            CreateMeetingStates.INPUT_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, input_meeting_url_handler),
            ],
            CreateMeetingStates.CONFIRM: [
                CallbackQueryHandler(confirm_meeting_callback, pattern=r"^(meeting_confirm:\w+|cancel)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(cancel_conversation, pattern="^cancel$"),
        ],
        per_message=False,
    )
