"""Telegram Bot initialization and management."""

import logging

from telegram import Bot, BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
)

from config.settings import get_settings

logger = logging.getLogger(__name__)

# Global bot instance
_application: Application | None = None
_bot: Bot | None = None


async def get_bot() -> Bot | None:
    """Get the bot instance."""
    global _bot
    settings = get_settings()

    if not settings.telegram_bot_token:
        return None

    if _bot is None:
        _bot = Bot(token=settings.telegram_bot_token)

    return _bot


def get_application() -> Application | None:
    """Get the application instance."""
    return _application


async def _set_bot_commands(bot: Bot):
    """Set bot commands for the menu."""
    commands = [
        BotCommand("start", "開始使用"),
        BotCommand("help", "顯示說明"),
        BotCommand("list", "排程:查看"),
        BotCommand("record", "排程:新增"),
        BotCommand("edit", "排程:編輯/刪除"),
        BotCommand("meetings", "會議:查看/新增"),
        BotCommand("stop", "停止錄製"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands menu configured")


async def start_bot():
    """Start the Telegram bot with polling mode."""
    global _application
    settings = get_settings()

    if not settings.telegram_bot_token:
        logger.warning("Telegram bot token not configured, skipping bot startup")
        return

    logger.info("Starting Telegram bot...")

    # Import handlers here to avoid circular imports
    from telegram_bot.handlers import setup_handlers

    # Build application
    _application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Setup handlers
    setup_handlers(_application)

    # Start polling (non-blocking)
    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling(drop_pending_updates=True)

    # Set bot commands (menu)
    await _set_bot_commands(_application.bot)

    logger.info("Telegram bot started in polling mode")


async def stop_bot():
    """Stop the Telegram bot."""
    global _application

    if _application is not None:
        logger.info("Stopping Telegram bot...")
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()
        _application = None
        logger.info("Telegram bot stopped")
