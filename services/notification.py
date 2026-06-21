"""Notification services for email, webhook, etc."""

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    """Configuration for notification services."""

    # Email settings
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: list[str] = None
    smtp_use_tls: bool = True

    # Webhook settings
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_secret: str = ""

    def __post_init__(self):
        if self.smtp_to is None:
            self.smtp_to = []


class _EmailNotifier:
    """Send email notifications via SMTP."""

    def __init__(self, config: NotificationConfig):
        self.config = config

    async def send(
        self,
        subject: str,
        body: str,
        html_body: str | None = None,
        to: list[str] | None = None,
    ) -> bool:
        """Send email notification."""
        if not self.config.smtp_enabled:
            logger.debug("Email notifications disabled")
            return False

        recipients = to or self.config.smtp_to
        if not recipients:
            logger.warning("No email recipients configured")
            return False

        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.config.smtp_from
            msg["To"] = ", ".join(recipients)

            # Add plain text
            msg.attach(MIMEText(body, "plain"))

            # Add HTML if provided
            if html_body:
                msg.attach(MIMEText(html_body, "html"))

            # Send in thread to avoid blocking
            await asyncio.get_event_loop().run_in_executor(None, self._send_smtp, msg, recipients)

            logger.info(f"Email sent to {recipients}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    def _send_smtp(self, msg: MIMEMultipart, recipients: list[str]) -> None:
        """Synchronous SMTP send."""
        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port) as server:
            if self.config.smtp_use_tls:
                server.starttls()
            if self.config.smtp_user and self.config.smtp_password:
                server.login(self.config.smtp_user, self.config.smtp_password)
            server.send_message(msg, to_addrs=recipients)


class _WebhookNotifier:
    """Send webhook notifications via HTTP POST."""

    def __init__(self, config: NotificationConfig):
        self.config = config

    async def send(
        self,
        event: str,
        payload: dict[str, Any],
    ) -> bool:
        """Send webhook notification."""
        if not self.config.webhook_enabled:
            logger.debug("Webhook notifications disabled")
            return False

        if not self.config.webhook_url:
            logger.warning("No webhook URL configured")
            return False

        try:
            data = {
                "event": event,
                "timestamp": asyncio.get_event_loop().time(),
                "payload": payload,
            }

            headers = {"Content-Type": "application/json"}

            # Add secret header if configured
            if self.config.webhook_secret:
                import hashlib
                import hmac
                import json

                body = json.dumps(data)
                signature = hmac.new(
                    self.config.webhook_secret.encode(),
                    body.encode(),
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Webhook-Signature"] = f"sha256={signature}"

            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self.config.webhook_url,
                    json=data,
                    headers=headers,
                )
                response.raise_for_status()

            logger.info(f"Webhook sent to {self.config.webhook_url}")
            return True

        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")
            return False


class NotificationService:
    """Configured email and webhook notification channels."""

    def __init__(self, config: NotificationConfig | None = None):
        self.config = config or NotificationConfig()
        self.email = _EmailNotifier(self.config)
        self.webhook = _WebhookNotifier(self.config)


def load_notification_config() -> NotificationConfig:
    """Load notification config from database or environment."""
    import json
    import os

    from database.models import AppSettings
    from database.session import get_session_local

    config = NotificationConfig()

    # Try loading from database first
    try:
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            record = session.query(AppSettings).filter(AppSettings.key == "notification_config").first()
            if record:
                data = json.loads(record.value)
                return NotificationConfig(
                    smtp_enabled=data.get("smtp_enabled", False),
                    smtp_host=data.get("smtp_host", ""),
                    smtp_port=data.get("smtp_port", 587),
                    smtp_user=data.get("smtp_user", ""),
                    smtp_password=data.get("smtp_password", ""),
                    smtp_from=data.get("smtp_from", ""),
                    smtp_to=data.get("smtp_to", []),
                    smtp_use_tls=data.get("smtp_use_tls", True),
                    webhook_enabled=data.get("webhook_enabled", False),
                    webhook_url=data.get("webhook_url", ""),
                    webhook_secret=data.get("webhook_secret", ""),
                )
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load notification config from database: {e}")

    # Fall back to environment variables
    config.smtp_enabled = os.getenv("SMTP_ENABLED", "").lower() == "true"
    config.smtp_host = os.getenv("SMTP_HOST", "")
    config.smtp_port = int(os.getenv("SMTP_PORT", "587"))
    config.smtp_user = os.getenv("SMTP_USER", "")
    config.smtp_password = os.getenv("SMTP_PASSWORD", "")
    config.smtp_from = os.getenv("SMTP_FROM", "")
    config.smtp_to = os.getenv("SMTP_TO", "").split(",") if os.getenv("SMTP_TO") else []
    config.webhook_enabled = os.getenv("WEBHOOK_ENABLED", "").lower() == "true"
    config.webhook_url = os.getenv("WEBHOOK_URL", "")
    config.webhook_secret = os.getenv("WEBHOOK_SECRET", "")

    return config


# Global service instance
_notification_service: NotificationService | None = None


def get_notification_service() -> NotificationService:
    """Get or create notification service."""
    global _notification_service
    if _notification_service is None:
        config = load_notification_config()
        _notification_service = NotificationService(config)
    return _notification_service


def reset_notification_service() -> None:
    """Clear the cached notification service so config is reloaded on next use."""
    global _notification_service
    _notification_service = None
