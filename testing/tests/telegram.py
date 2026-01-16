"""Telegram test implementation."""

from config.settings import get_settings
from testing.models import TestResult
from testing.tests.base import BaseTest


class TelegramTest(BaseTest):
    """Test Telegram bot configuration and connectivity."""

    name = "Telegram Test"
    description = "Check Telegram bot status and optionally send test message"

    def __init__(
        self,
        send_test_message: bool = False,
        target_chat_id: int | None = None,
    ) -> None:
        super().__init__()
        self.send_test_message = send_test_message
        self.target_chat_id = target_chat_id

    async def run(self) -> TestResult:
        """Run Telegram test."""
        results = {}

        # Check configuration
        self.log("Checking Telegram configuration...")
        settings = get_settings()

        if not settings.telegram_bot_token:
            self.log("Telegram bot token not configured", "ERROR")
            return TestResult(
                success=False,
                error="Bot token not configured",
                data={"configured": False},
            )

        results["configured"] = True
        self.log("Bot token is configured")

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Get bot info
        self.log("Getting bot info...")
        try:
            from telegram_bot.bot import get_bot

            bot = await get_bot()
            if not bot:
                return TestResult(
                    success=False,
                    error="Failed to create bot instance",
                )

            bot_info = await bot.get_me()
            results["bot_username"] = bot_info.username
            results["bot_id"] = bot_info.id
            results["bot_name"] = bot_info.first_name
            self.log(f"Bot: @{bot_info.username} ({bot_info.first_name})", "SUCCESS")

        except Exception as e:
            self.log(f"Failed to get bot info: {e}", "ERROR")
            return TestResult(
                success=False,
                error=f"Failed to get bot info: {e}",
                data=results,
            )

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Check approved users
        self.log("Checking approved users...")
        try:
            from database.repository import get_telegram_users
            from database.session import get_db

            db = next(get_db())
            users = get_telegram_users(db)
            approved_users = [u for u in users if u.approved]
            pending_users = [u for u in users if not u.approved]

            results["total_users"] = len(users)
            results["approved_users"] = len(approved_users)
            results["pending_users"] = len(pending_users)

            self.log(f"Users: {len(approved_users)} approved, {len(pending_users)} pending")

        except Exception as e:
            self.log(f"Failed to check users: {e}", "WARNING")
            results["users_error"] = str(e)

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Send test message if requested
        if self.send_test_message:
            await self._send_test_message(bot, results)

        self.log("Telegram test completed", "SUCCESS")
        return TestResult(success=True, data=results)

    async def _send_test_message(self, bot, results: dict) -> None:
        """Send test message to target or all approved users."""
        self.log("Sending test message...")

        try:
            from database.repository import get_telegram_users
            from database.session import get_db

            if self.target_chat_id:
                # Send to specific chat
                await bot.send_message(
                    chat_id=self.target_chat_id,
                    text="Test message from Meeting Recorder",
                )
                self.log(f"Test message sent to {self.target_chat_id}", "SUCCESS")
                results["message_sent_to"] = [self.target_chat_id]
            else:
                # Send to first approved user
                db = next(get_db())
                users = get_telegram_users(db)
                approved = [u for u in users if u.approved]

                if approved:
                    user = approved[0]
                    await bot.send_message(
                        chat_id=user.chat_id,
                        text="Test message from Meeting Recorder",
                    )
                    self.log(f"Test message sent to {user.chat_id}", "SUCCESS")
                    results["message_sent_to"] = [user.chat_id]
                else:
                    self.log("No approved users to send test message", "WARNING")
                    results["message_sent_to"] = []

        except Exception as e:
            self.log(f"Failed to send test message: {e}", "ERROR")
            results["message_error"] = str(e)
