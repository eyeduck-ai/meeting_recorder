"""Meeting end detection framework.

This module provides a pluggable detection system for identifying
when a meeting has ended.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


class DetectorType(str, Enum):
    """Types of meeting end detectors."""

    TEXT_INDICATOR = "text_indicator"
    VIDEO_ELEMENT = "video_element"
    WEBRTC_CONNECTION = "webrtc_connection"
    SCREEN_FREEZE = "screen_freeze"
    AUDIO_SILENCE = "audio_silence"
    URL_CHANGE = "url_change"


@dataclass
class DetectionResult:
    """Result from a detector check."""

    detector_type: DetectorType
    detected: bool
    confidence: float = 1.0  # 0.0 to 1.0
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DetectionConfig:
    """Configuration for the detection system."""

    # Enable/disable individual detectors
    text_indicator_enabled: bool = True
    video_element_enabled: bool = True
    webrtc_connection_enabled: bool = True
    screen_freeze_enabled: bool = False
    audio_silence_enabled: bool = False
    url_change_enabled: bool = True

    # Thresholds
    screen_freeze_threshold: float = 0.98  # Similarity threshold
    screen_freeze_timeout_sec: int = 60
    audio_silence_timeout_sec: int = 120
    audio_silence_threshold: float = 0.05  # Volume level below this is considered silence

    # Voting configuration
    min_detectors_agree: int = 1  # Minimum detectors that must agree

    # Priority (lower = higher priority)
    priorities: dict = field(default_factory=lambda: {
        DetectorType.WEBRTC_CONNECTION: 1,
        DetectorType.TEXT_INDICATOR: 2,
        DetectorType.VIDEO_ELEMENT: 3,
        DetectorType.URL_CHANGE: 4,
        DetectorType.SCREEN_FREEZE: 5,
        DetectorType.AUDIO_SILENCE: 6,
    })

    def is_detector_enabled(self, detector_type: DetectorType) -> bool:
        """Check if a detector is enabled."""
        mapping = {
            DetectorType.TEXT_INDICATOR: self.text_indicator_enabled,
            DetectorType.VIDEO_ELEMENT: self.video_element_enabled,
            DetectorType.WEBRTC_CONNECTION: self.webrtc_connection_enabled,
            DetectorType.SCREEN_FREEZE: self.screen_freeze_enabled,
            DetectorType.AUDIO_SILENCE: self.audio_silence_enabled,
            DetectorType.URL_CHANGE: self.url_change_enabled,
        }
        return mapping.get(detector_type, False)


class DetectorBase(ABC):
    """Abstract base class for all meeting end detectors."""

    def __init__(self, config: DetectionConfig):
        self.config = config
        self._last_check_time: datetime | None = None

    @property
    @abstractmethod
    def detector_type(self) -> DetectorType:
        """Return the detector type."""
        pass

    @property
    def priority(self) -> int:
        """Return detector priority from config."""
        return self.config.priorities.get(self.detector_type, 99)

    @property
    def is_enabled(self) -> bool:
        """Check if this detector is enabled."""
        return self.config.is_detector_enabled(self.detector_type)

    @abstractmethod
    async def check(self, page: "Page") -> DetectionResult:
        """Check if meeting has ended.

        Args:
            page: Playwright page instance

        Returns:
            DetectionResult with detection status
        """
        pass

    async def setup(self, page: "Page") -> None:
        """Optional setup method called before recording starts.

        Override this for detectors that need to inject JS or initialize state.
        """
        pass

    def reset(self) -> None:
        """Reset detector state between recordings."""
        self._last_check_time = None


class DetectionOrchestrator:
    """Orchestrates multiple detectors and makes final decisions."""

    def __init__(self, config: DetectionConfig | None = None):
        self.config = config or DetectionConfig()
        self.detectors: list[DetectorBase] = []
        self.detection_log: list[DetectionResult] = []
        self._dry_run: bool = False

    def register_detector(self, detector: DetectorBase) -> None:
        """Register a detector with the orchestrator."""
        self.detectors.append(detector)
        # Sort by priority
        self.detectors.sort(key=lambda d: d.priority)
        logger.debug(f"Registered detector: {detector.detector_type.value}")

    def set_dry_run(self, enabled: bool) -> None:
        """Enable/disable dry run mode (log only, don't stop recording)."""
        self._dry_run = enabled

    async def setup_all(self, page: "Page") -> None:
        """Setup all enabled detectors."""
        for detector in self.detectors:
            if detector.is_enabled:
                try:
                    await detector.setup(page)
                except Exception as e:
                    logger.warning(f"Failed to setup {detector.detector_type}: {e}")

    async def check_all(self, page: "Page") -> tuple[bool, list[DetectionResult]]:
        """Check all enabled detectors.

        Returns:
            Tuple of (should_end, results)
        """
        results: list[DetectionResult] = []
        triggered_count = 0

        for detector in self.detectors:
            if not detector.is_enabled:
                continue

            try:
                result = await detector.check(page)
                results.append(result)
                self.detection_log.append(result)

                if result.detected:
                    triggered_count += 1
                    logger.info(
                        f"Detector {result.detector_type.value} triggered: {result.reason}"
                    )

                    # In non-dry-run mode, check if we have enough agreement
                    if not self._dry_run and triggered_count >= self.config.min_detectors_agree:
                        return True, results

            except Exception as e:
                logger.debug(f"Detector {detector.detector_type} error: {e}")

        # Check voting threshold
        should_end = triggered_count >= self.config.min_detectors_agree
        if self._dry_run:
            should_end = False  # Never end in dry run mode

        return should_end, results

    def reset_all(self) -> None:
        """Reset all detectors."""
        for detector in self.detectors:
            detector.reset()
        self.detection_log.clear()

    def get_log_summary(self) -> list[dict]:
        """Get summary of detection log for analysis."""
        return [
            {
                "detector": r.detector_type.value,
                "detected": r.detected,
                "confidence": r.confidence,
                "reason": r.reason,
                "timestamp": r.timestamp.isoformat(),
            }
            for r in self.detection_log
        ]
