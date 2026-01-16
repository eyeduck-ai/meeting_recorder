"""Base class for all tests."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from testing.models import TestResult


class BaseTest(ABC):
    """Abstract base class for all test implementations."""

    name: str = "Base Test"
    description: str = "Base test description"

    def __init__(self) -> None:
        self._log_callback: Callable[[str, str], None] | None = None
        self._cancelled: bool = False

    def set_log_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set the callback function for logging.

        Args:
            callback: Function that takes (message, level) as arguments
        """
        self._log_callback = callback

    def log(self, message: str, level: str = "INFO") -> None:
        """Log a message.

        Args:
            message: The message to log
            level: Log level (INFO, WARNING, ERROR, SUCCESS)
        """
        if self._log_callback:
            self._log_callback(message, level)

    def cancel(self) -> None:
        """Request cancellation of the test."""
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._cancelled

    @abstractmethod
    async def run(self) -> TestResult:
        """Execute the test.

        Returns:
            TestResult with success status and optional data/error
        """
        pass

    async def cleanup(self) -> None:
        """Clean up resources after test execution.

        Override this method to implement cleanup logic.
        """
        return
