"""Tests for meeting end detection framework."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from recording.detection import (
    DetectionConfig,
    DetectionOrchestrator,
    DetectionResult,
    DetectorType,
)
from recording.detectors import (
    ScreenFreezeDetector,
    TextIndicatorDetector,
    URLChangeDetector,
    VideoElementDetector,
    WebRTCConnectionDetector,
    create_default_detectors,
)


class MockPage:
    """Mock Playwright page for testing."""

    def __init__(self):
        self.url = "https://meet.jit.si/test-room"
        self._locator_results = {}
        self._evaluate_results = {}
        self._screenshot_data = b"fake_screenshot_data"

    def locator(self, selector: str):
        """Return mock locator."""
        mock_locator = MagicMock()
        count = self._locator_results.get(selector, 0)
        mock_locator.count = AsyncMock(return_value=count)
        return mock_locator

    async def evaluate(self, script: str):
        """Mock evaluate."""
        for key, value in self._evaluate_results.items():
            if key in script:
                return value
        return None

    async def screenshot(self, **kwargs):
        """Mock screenshot."""
        return self._screenshot_data


@pytest.fixture
def detection_config():
    """Create default detection config."""
    return DetectionConfig()


@pytest.fixture
def mock_page():
    """Create mock page."""
    return MockPage()


# ============================================================================
# TextIndicatorDetector Tests
# ============================================================================

class TestTextIndicatorDetector:
    """Tests for TextIndicatorDetector."""

    @pytest.mark.asyncio
    async def test_detects_meeting_ended_english(self, detection_config, mock_page):
        """Should detect 'Meeting has ended' text."""
        detector = TextIndicatorDetector(detection_config)
        mock_page._locator_results['text="Meeting has ended"'] = 1

        result = await detector.check(mock_page)

        assert result.detected is True
        assert result.detector_type == DetectorType.TEXT_INDICATOR
        assert "Meeting has ended" in result.reason

    @pytest.mark.asyncio
    async def test_detects_meeting_ended_chinese(self, detection_config, mock_page):
        """Should detect Chinese meeting ended text."""
        detector = TextIndicatorDetector(detection_config)
        mock_page._locator_results['text="會議已結束"'] = 1

        result = await detector.check(mock_page)

        assert result.detected is True

    @pytest.mark.asyncio
    async def test_no_detection_when_no_indicators(self, detection_config, mock_page):
        """Should not detect when no end indicators present."""
        detector = TextIndicatorDetector(detection_config)
        # All locator results default to 0

        result = await detector.check(mock_page)

        assert result.detected is False

    @pytest.mark.asyncio
    async def test_detects_disconnected(self, detection_config, mock_page):
        """Should detect disconnection message."""
        detector = TextIndicatorDetector(detection_config)
        mock_page._locator_results['text="You have been disconnected"'] = 1

        result = await detector.check(mock_page)

        assert result.detected is True


# ============================================================================
# VideoElementDetector Tests
# ============================================================================

class TestVideoElementDetector:
    """Tests for VideoElementDetector."""

    @pytest.mark.asyncio
    async def test_no_detection_when_video_present(self, detection_config, mock_page):
        """Should not detect when video elements are present."""
        detector = VideoElementDetector(detection_config)
        mock_page._locator_results["video"] = 2

        result = await detector.check(mock_page)

        assert result.detected is False

    @pytest.mark.asyncio
    async def test_detection_when_video_gone_after_delay(self, detection_config, mock_page):
        """Should detect when video elements disappear for 5+ seconds."""
        detector = VideoElementDetector(detection_config)
        mock_page._locator_results["video"] = 0

        # First check - starts timer
        result1 = await detector.check(mock_page)
        assert result1.detected is False

        # Simulate time passing by setting _no_video_since in the past
        detector._no_video_since = asyncio.get_event_loop().time() - 10

        # Second check - should trigger
        result2 = await detector.check(mock_page)
        assert result2.detected is True

    @pytest.mark.asyncio
    async def test_reset_clears_timer(self, detection_config, mock_page):
        """Reset should clear the no-video timer."""
        detector = VideoElementDetector(detection_config)
        detector._no_video_since = 12345.0

        detector.reset()

        assert detector._no_video_since is None


# ============================================================================
# URLChangeDetector Tests
# ============================================================================

class TestURLChangeDetector:
    """Tests for URLChangeDetector."""

    @pytest.mark.asyncio
    async def test_setup_records_initial_url(self, detection_config, mock_page):
        """Setup should record initial URL."""
        detector = URLChangeDetector(detection_config)
        mock_page.url = "https://meet.jit.si/test-room"

        await detector.setup(mock_page)

        assert detector._initial_url == "https://meet.jit.si/test-room"

    @pytest.mark.asyncio
    async def test_no_detection_when_still_on_meeting(self, detection_config, mock_page):
        """Should not detect when still on meeting domain."""
        detector = URLChangeDetector(detection_config)
        mock_page.url = "https://meet.jit.si/test-room"
        await detector.setup(mock_page)

        result = await detector.check(mock_page)

        assert result.detected is False

    @pytest.mark.asyncio
    async def test_detection_when_navigated_away(self, detection_config, mock_page):
        """Should detect when navigated away from meeting domain."""
        detector = URLChangeDetector(detection_config)
        mock_page.url = "https://meet.jit.si/test-room"
        await detector.setup(mock_page)

        # Simulate navigation away
        mock_page.url = "https://google.com"

        result = await detector.check(mock_page)

        assert result.detected is True
        assert "meet.jit.si" in result.reason


# ============================================================================
# WebRTCConnectionDetector Tests
# ============================================================================

class TestWebRTCConnectionDetector:
    """Tests for WebRTCConnectionDetector."""

    @pytest.mark.asyncio
    async def test_setup_injects_script(self, detection_config, mock_page):
        """Setup should inject WebRTC monitoring script."""
        detector = WebRTCConnectionDetector(detection_config)

        await detector.setup(mock_page)

        assert detector._injected is True

    @pytest.mark.asyncio
    async def test_no_detection_when_connection_active(self, detection_config, mock_page):
        """Should not detect when WebRTC connection is active."""
        detector = WebRTCConnectionDetector(detection_config)
        await detector.setup(mock_page)
        mock_page._evaluate_results["_rtcConnectionLost"] = False

        result = await detector.check(mock_page)

        assert result.detected is False

    @pytest.mark.asyncio
    async def test_detection_when_connection_lost(self, detection_config, mock_page):
        """Should detect when WebRTC connection is lost."""
        detector = WebRTCConnectionDetector(detection_config)
        await detector.setup(mock_page)
        mock_page._evaluate_results["_rtcConnectionLost"] = True

        result = await detector.check(mock_page)

        assert result.detected is True
        assert "connection lost" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_no_detection_if_not_injected(self, detection_config, mock_page):
        """Should not detect if script was not injected."""
        detector = WebRTCConnectionDetector(detection_config)
        # Don't call setup

        result = await detector.check(mock_page)

        assert result.detected is False
        assert result.confidence == 0.0


# ============================================================================
# ScreenFreezeDetector Tests
# ============================================================================

class TestScreenFreezeDetector:
    """Tests for ScreenFreezeDetector."""

    @pytest.mark.asyncio
    async def test_no_detection_on_first_check(self, detection_config, mock_page):
        """First check should never detect (no baseline)."""
        detector = ScreenFreezeDetector(detection_config)

        result = await detector.check(mock_page)

        assert result.detected is False

    @pytest.mark.asyncio
    async def test_no_detection_when_screen_changing(self, detection_config, mock_page):
        """Should not detect when screenshots are different."""
        detector = ScreenFreezeDetector(detection_config)

        # First check
        await detector.check(mock_page)

        # Change screenshot data
        mock_page._screenshot_data = b"different_data_completely_new"

        result = await detector.check(mock_page)

        assert result.detected is False

    @pytest.mark.asyncio
    async def test_detection_when_frozen_long_enough(self, detection_config, mock_page):
        """Should detect when screen is frozen for timeout duration."""
        detection_config.screen_freeze_timeout_sec = 1  # Short timeout for test
        detection_config.screen_freeze_threshold = 0.5  # Low threshold for test
        detector = ScreenFreezeDetector(detection_config)

        # Use longer data for proper comparison (algorithm samples 1000 bytes)
        mock_page._screenshot_data = b"X" * 2000

        # First check - establish baseline with same screenshot
        await detector.check(mock_page)

        # Second check with identical screenshot - starts timer
        result2 = await detector.check(mock_page)
        # Should not trigger yet (timer just started)
        assert result2.detected is False

        # Simulate time passing by backdating freeze_start
        detector._freeze_start = asyncio.get_event_loop().time() - 10

        # Third check - should trigger (frozen long enough)
        result3 = await detector.check(mock_page)

        assert result3.detected is True
        assert "frozen" in result3.reason.lower()

    @pytest.mark.asyncio
    async def test_reset_clears_state(self, detection_config, mock_page):
        """Reset should clear all state."""
        detector = ScreenFreezeDetector(detection_config)
        detector._last_screenshot = b"data"
        detector._freeze_start = 12345.0

        detector.reset()

        assert detector._last_screenshot is None
        assert detector._freeze_start is None


# ============================================================================
# DetectionOrchestrator Tests
# ============================================================================

class TestDetectionOrchestrator:
    """Tests for DetectionOrchestrator."""

    def test_register_detector_sorts_by_priority(self, detection_config):
        """Detectors should be sorted by priority after registration."""
        orchestrator = DetectionOrchestrator(detection_config)
        
        # Register in wrong order
        orchestrator.register_detector(ScreenFreezeDetector(detection_config))  # Priority 5
        orchestrator.register_detector(WebRTCConnectionDetector(detection_config))  # Priority 1

        assert orchestrator.detectors[0].detector_type == DetectorType.WEBRTC_CONNECTION
        assert orchestrator.detectors[1].detector_type == DetectorType.SCREEN_FREEZE

    @pytest.mark.asyncio
    async def test_check_all_returns_results(self, detection_config, mock_page):
        """check_all should return results from all enabled detectors."""
        orchestrator = DetectionOrchestrator(detection_config)
        orchestrator.register_detector(TextIndicatorDetector(detection_config))

        should_end, results = await orchestrator.check_all(mock_page)

        assert isinstance(results, list)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_dry_run_mode_never_ends(self, detection_config, mock_page):
        """Dry run mode should never trigger end."""
        orchestrator = DetectionOrchestrator(detection_config)
        orchestrator.set_dry_run(True)
        
        detector = TextIndicatorDetector(detection_config)
        orchestrator.register_detector(detector)
        mock_page._locator_results['text="Meeting has ended"'] = 1

        should_end, results = await orchestrator.check_all(mock_page)

        assert should_end is False  # Dry run never ends
        # But detection should still be logged
        triggered = [r for r in results if r.detected]
        assert len(triggered) >= 1

    def test_min_detectors_agree_threshold(self, detection_config):
        """Should respect min_detectors_agree setting."""
        detection_config.min_detectors_agree = 2
        orchestrator = DetectionOrchestrator(detection_config)

        assert orchestrator.config.min_detectors_agree == 2


# ============================================================================
# Factory Function Tests
# ============================================================================

class TestCreateDefaultDetectors:
    """Tests for create_default_detectors factory."""

    def test_creates_all_detectors(self, detection_config):
        """Should create all default detector instances."""
        detectors = create_default_detectors(detection_config)

        assert len(detectors) == 5
        
        types = {d.detector_type for d in detectors}
        assert DetectorType.TEXT_INDICATOR in types
        assert DetectorType.VIDEO_ELEMENT in types
        assert DetectorType.URL_CHANGE in types
        assert DetectorType.WEBRTC_CONNECTION in types
        assert DetectorType.SCREEN_FREEZE in types

    def test_uses_provided_config(self, detection_config):
        """Should use the provided config for all detectors."""
        detection_config.screen_freeze_timeout_sec = 999
        detectors = create_default_detectors(detection_config)

        for detector in detectors:
            assert detector.config.screen_freeze_timeout_sec == 999


# ============================================================================
# Detection Config Tests
# ============================================================================

class TestDetectionConfig:
    """Tests for DetectionConfig."""

    def test_default_values(self):
        """Should have sensible defaults."""
        config = DetectionConfig()

        assert config.text_indicator_enabled is True
        assert config.video_element_enabled is True
        assert config.webrtc_connection_enabled is True
        assert config.screen_freeze_enabled is False
        assert config.audio_silence_enabled is False
        assert config.min_detectors_agree == 1

    def test_is_detector_enabled(self):
        """is_detector_enabled should return correct values."""
        config = DetectionConfig(
            text_indicator_enabled=True,
            screen_freeze_enabled=False,
        )

        assert config.is_detector_enabled(DetectorType.TEXT_INDICATOR) is True
        assert config.is_detector_enabled(DetectorType.SCREEN_FREEZE) is False

    def test_priorities(self):
        """Priorities should be set correctly."""
        config = DetectionConfig()

        assert config.priorities[DetectorType.WEBRTC_CONNECTION] == 1
        assert config.priorities[DetectorType.TEXT_INDICATOR] == 2
