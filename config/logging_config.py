"""Logging configuration with file rotation support.

This module configures logging to write to both console and a rotating log file.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import get_settings


def setup_logging() -> None:
    """Configure logging with console and file handlers.

    Sets up:
    - Console handler (stdout) with INFO level
    - Rotating file handler (logs/app.log) with 50MB size limit and 10 backups
    """
    settings = get_settings()
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clear any existing handlers (avoid duplicates)
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)

    # File handler with rotation
    log_dir = Path(settings.logs_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(file_handler)

    logging.info(f"Log file: {log_file}")
