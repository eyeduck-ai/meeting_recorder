"""Data models for the testing module."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TestStatus(str, Enum):
    """Status of a test run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TestType(str, Enum):
    """Types of available tests."""

    ENVIRONMENT = "environment"
    BROWSER = "browser"
    PROVIDER = "provider"
    RECORDING = "recording"
    TELEGRAM = "telegram"
    YOUTUBE = "youtube"


@dataclass
class TestResult:
    """Result of a test execution."""

    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class LogEntry:
    """A single log entry."""

    timestamp: datetime
    message: str
    level: str = "INFO"

    def format(self) -> str:
        """Format log entry for display."""
        ts = self.timestamp.strftime("%H:%M:%S")
        return f"[{ts}] {self.message}"


@dataclass
class TestRun:
    """Represents a single test execution."""

    test_id: str
    test_type: TestType
    status: TestStatus = TestStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    logs: list[LogEntry] = field(default_factory=list)
    result: TestResult | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def add_log(self, message: str, level: str = "INFO") -> None:
        """Add a log entry."""
        self.logs.append(LogEntry(timestamp=datetime.now(), message=message, level=level))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "test_id": self.test_id,
            "test_type": self.test_type.value,
            "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "logs": [
                {"timestamp": log.timestamp.isoformat(), "message": log.message, "level": log.level}
                for log in self.logs
            ],
            "result": {
                "success": self.result.success,
                "data": self.result.data,
                "error": self.result.error,
            }
            if self.result
            else None,
            "params": self.params,
        }
