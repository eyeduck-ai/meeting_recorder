"""Concrete detector implementations.

This module contains all individual detector implementations
that plug into the detection framework.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

from recording.detection import DetectionConfig, DetectionResult, DetectorBase, DetectorType

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


class TextIndicatorDetector(DetectorBase):
    """Detects meeting end via text indicators on the page."""

    # Common end indicators in English and Chinese
    END_INDICATORS = [
        # Meeting ended
        'text="meeting has ended"',
        'text="Meeting has ended"',
        'text="會議已結束"',
        ':text("Meeting has ended")',
        ':text("會議已結束")',
        # Disconnected
        'text="You have been disconnected"',
        'text="連線已中斷"',
        'text="disconnected"',
        ':text("disconnected")',
        ':text("連線已中斷")',
        # Left meeting
        'text="You have left the meeting"',
        ':text("You have left the meeting")',
        ':text("已離開會議")',
        # Host ended
        'text="The host ended the meeting"',
        ':text("The host ended the meeting")',
        ':text("主持人已結束會議")',
        # Kicked
        'text="kicked"',
        'text="removed from the meeting"',
        # Not found
        'text="Conference not found"',
        'text="會議不存在"',
        ':text("Meeting unavailable")',
    ]

    @property
    def detector_type(self) -> DetectorType:
        return DetectorType.TEXT_INDICATOR

    async def check(self, page: "Page") -> DetectionResult:
        """Check for text indicators of meeting end."""
        try:
            for indicator in self.END_INDICATORS:
                count = await page.locator(indicator).count()
                if count > 0:
                    return DetectionResult(
                        detector_type=self.detector_type,
                        detected=True,
                        confidence=1.0,
                        reason=f"Found text indicator: {indicator}",
                    )
        except Exception as e:
            logger.debug(f"TextIndicatorDetector error: {e}")

        return DetectionResult(
            detector_type=self.detector_type,
            detected=False,
            reason="No end indicators found",
        )


class VideoElementDetector(DetectorBase):
    """Detects meeting end when video elements disappear."""

    def __init__(self, config: DetectionConfig):
        super().__init__(config)
        self._no_video_since: float | None = None

    @property
    def detector_type(self) -> DetectorType:
        return DetectorType.VIDEO_ELEMENT

    async def check(self, page: "Page") -> DetectionResult:
        """Check if video elements have disappeared."""
        try:
            video_count = await page.locator("video").count()

            if video_count == 0:
                if self._no_video_since is None:
                    self._no_video_since = asyncio.get_event_loop().time()
                else:
                    # Require 5 seconds of no video to confirm
                    elapsed = asyncio.get_event_loop().time() - self._no_video_since
                    if elapsed >= 5:
                        return DetectionResult(
                            detector_type=self.detector_type,
                            detected=True,
                            confidence=0.9,
                            reason=f"No video elements for {elapsed:.1f}s",
                        )
            else:
                self._no_video_since = None

        except Exception as e:
            logger.debug(f"VideoElementDetector error: {e}")

        return DetectionResult(
            detector_type=self.detector_type,
            detected=False,
            reason=f"Video elements present",
        )

    def reset(self) -> None:
        super().reset()
        self._no_video_since = None


class URLChangeDetector(DetectorBase):
    """Detects meeting end when URL changes away from meeting page."""

    MEETING_DOMAINS = [
        "meet.jit.si",
        "webex.com",
        "zoom.us",
        "teams.microsoft.com",
    ]

    def __init__(self, config: DetectionConfig):
        super().__init__(config)
        self._initial_url: str | None = None

    @property
    def detector_type(self) -> DetectorType:
        return DetectorType.URL_CHANGE

    async def setup(self, page: "Page") -> None:
        """Record initial URL."""
        self._initial_url = page.url

    async def check(self, page: "Page") -> DetectionResult:
        """Check if URL has changed away from meeting."""
        current_url = page.url

        # Check if we're still on a meeting domain
        for domain in self.MEETING_DOMAINS:
            if domain in (self._initial_url or ""):
                if domain not in current_url:
                    return DetectionResult(
                        detector_type=self.detector_type,
                        detected=True,
                        confidence=1.0,
                        reason=f"Navigated away from {domain}",
                    )

        return DetectionResult(
            detector_type=self.detector_type,
            detected=False,
            reason="Still on meeting domain",
        )

    def reset(self) -> None:
        super().reset()
        self._initial_url = None


class WebRTCConnectionDetector(DetectorBase):
    """Detects meeting end via WebRTC connection state changes."""

    JS_INJECT_SCRIPT = """
    window._rtcConnectionLost = false;
    window._rtcConnectionChecked = true;
    
    if (window.RTCPeerConnection && !window._rtcPatched) {
        window._rtcPatched = true;
        const OriginalRTCPeerConnection = window.RTCPeerConnection;
        
        window.RTCPeerConnection = function(...args) {
            const pc = new OriginalRTCPeerConnection(...args);
            
            pc.addEventListener('connectionstatechange', () => {
                if (pc.connectionState === 'disconnected' || 
                    pc.connectionState === 'failed' ||
                    pc.connectionState === 'closed') {
                    console.log('[RTCDetector] Connection state:', pc.connectionState);
                    window._rtcConnectionLost = true;
                }
            });
            
            pc.addEventListener('iceconnectionstatechange', () => {
                if (pc.iceConnectionState === 'disconnected' || 
                    pc.iceConnectionState === 'failed' ||
                    pc.iceConnectionState === 'closed') {
                    console.log('[RTCDetector] ICE state:', pc.iceConnectionState);
                    window._rtcConnectionLost = true;
                }
            });
            
            return pc;
        };
        
        // Copy static properties
        Object.assign(window.RTCPeerConnection, OriginalRTCPeerConnection);
        window.RTCPeerConnection.prototype = OriginalRTCPeerConnection.prototype;
    }
    """

    def __init__(self, config: DetectionConfig):
        super().__init__(config)
        self._injected = False

    @property
    def detector_type(self) -> DetectorType:
        return DetectorType.WEBRTC_CONNECTION

    async def setup(self, page: "Page") -> None:
        """Inject WebRTC monitoring script."""
        try:
            await page.evaluate(self.JS_INJECT_SCRIPT)
            self._injected = True
            logger.info("WebRTC connection monitoring injected")
        except Exception as e:
            logger.warning(f"Failed to inject WebRTC monitor: {e}")
            self._injected = False

    async def check(self, page: "Page") -> DetectionResult:
        """Check if WebRTC connection has been lost."""
        if not self._injected:
            return DetectionResult(
                detector_type=self.detector_type,
                detected=False,
                confidence=0.0,
                reason="WebRTC monitoring not injected",
            )

        try:
            is_lost = await page.evaluate("window._rtcConnectionLost === true")
            if is_lost:
                return DetectionResult(
                    detector_type=self.detector_type,
                    detected=True,
                    confidence=1.0,
                    reason="WebRTC connection lost",
                )
        except Exception as e:
            logger.debug(f"WebRTCConnectionDetector error: {e}")

        return DetectionResult(
            detector_type=self.detector_type,
            detected=False,
            reason="WebRTC connection active",
        )

    def reset(self) -> None:
        super().reset()
        self._injected = False


class ScreenFreezeDetector(DetectorBase):
    """Detects meeting end when screen is frozen for too long."""

    def __init__(self, config: DetectionConfig):
        super().__init__(config)
        self._last_screenshot: bytes | None = None
        self._freeze_start: float | None = None

    @property
    def detector_type(self) -> DetectorType:
        return DetectorType.SCREEN_FREEZE

    async def check(self, page: "Page") -> DetectionResult:
        """Check if screen has been frozen."""
        try:
            current = await page.screenshot(type="jpeg", quality=50)

            if self._last_screenshot:
                similarity = self._compare_screenshots(self._last_screenshot, current)

                if similarity > self.config.screen_freeze_threshold:
                    if self._freeze_start is None:
                        self._freeze_start = asyncio.get_event_loop().time()
                    else:
                        elapsed = asyncio.get_event_loop().time() - self._freeze_start
                        if elapsed >= self.config.screen_freeze_timeout_sec:
                            return DetectionResult(
                                detector_type=self.detector_type,
                                detected=True,
                                confidence=similarity,
                                reason=f"Screen frozen for {elapsed:.0f}s (similarity: {similarity:.2%})",
                            )
                else:
                    self._freeze_start = None

            self._last_screenshot = current

        except Exception as e:
            logger.debug(f"ScreenFreezeDetector error: {e}")

        return DetectionResult(
            detector_type=self.detector_type,
            detected=False,
            reason="Screen is active",
        )

    def _compare_screenshots(self, img1: bytes, img2: bytes) -> float:
        """Compare two screenshots and return similarity (0-1).
        
        Uses PIL to compute normalized pixel difference.
        Higher values mean more similar images.
        """
        try:
            from io import BytesIO

            from PIL import Image

            # Load images from bytes
            image1 = Image.open(BytesIO(img1))
            image2 = Image.open(BytesIO(img2))

            # Resize to small size for faster comparison (and normalize sizes)
            size = (100, 75)  # Small size for fast comparison
            image1 = image1.resize(size).convert("L")  # Convert to grayscale
            image2 = image2.resize(size).convert("L")

            # Get pixel data
            pixels1 = list(image1.getdata())
            pixels2 = list(image2.getdata())

            if len(pixels1) != len(pixels2):
                return 0.5

            # Calculate normalized mean squared difference
            total_pixels = len(pixels1)
            diff_sum = sum(abs(p1 - p2) for p1, p2 in zip(pixels1, pixels2))
            
            # Max possible difference per pixel is 255
            max_diff = 255 * total_pixels
            similarity = 1.0 - (diff_sum / max_diff)

            return similarity

        except Exception as e:
            logger.debug(f"PIL comparison failed, using fallback: {e}")
            # Fallback to simple size comparison
            if len(img1) == len(img2):
                return 0.9  # Same size suggests similar
            return 0.5

    def reset(self) -> None:
        super().reset()
        self._last_screenshot = None
        self._freeze_start = None


class AudioSilenceDetector(DetectorBase):
    """Detects meeting end when audio is silent for too long.
    
    This detector monitors audio levels via the FFmpeg process or
    PulseAudio to detect prolonged silence that may indicate
    the meeting has ended.
    """

    def __init__(self, config: DetectionConfig):
        super().__init__(config)
        self._silence_start: float | None = None
        self._last_audio_level: float = 0.0
        self._audio_source: str | None = None

    @property
    def detector_type(self) -> DetectorType:
        return DetectorType.AUDIO_SILENCE

    def set_audio_source(self, source: str) -> None:
        """Set the PulseAudio source to monitor."""
        self._audio_source = source

    async def check(self, page: "Page") -> DetectionResult:
        """Check if audio has been silent for too long.
        
        Note: This detector doesn't use the page object directly,
        it monitors the audio source set via set_audio_source().
        """
        if not self._audio_source:
            return DetectionResult(
                detector_type=self.detector_type,
                detected=False,
                confidence=0.0,
                reason="No audio source configured",
            )

        try:
            # Get current audio level
            audio_level = await self._get_audio_level()
            self._last_audio_level = audio_level

            # Check if audio is below silence threshold
            if audio_level < self.config.audio_silence_threshold:
                if self._silence_start is None:
                    self._silence_start = asyncio.get_event_loop().time()
                else:
                    elapsed = asyncio.get_event_loop().time() - self._silence_start
                    if elapsed >= self.config.audio_silence_timeout_sec:
                        return DetectionResult(
                            detector_type=self.detector_type,
                            detected=True,
                            confidence=0.8,
                            reason=f"Audio silent for {elapsed:.0f}s (level: {audio_level:.3f})",
                        )
            else:
                # Audio detected, reset silence timer
                self._silence_start = None

        except Exception as e:
            logger.debug(f"AudioSilenceDetector error: {e}")

        return DetectionResult(
            detector_type=self.detector_type,
            detected=False,
            reason=f"Audio active (level: {self._last_audio_level:.3f})",
        )

    async def _get_audio_level(self) -> float:
        """Get current audio level from PulseAudio source.
        
        Returns value between 0.0 (silence) and 1.0 (max volume).
        """
        import subprocess

        try:
            # Use pactl to get volume info
            # This is a lightweight check that doesn't require ffmpeg
            result = subprocess.run(
                ["pactl", "get-source-volume", self._audio_source],
                capture_output=True,
                text=True,
                timeout=1,
            )
            
            if result.returncode == 0:
                # Parse output like "Volume: front-left: 65536 / 100%"
                output = result.stdout
                if "%" in output:
                    # Extract percentage
                    import re
                    match = re.search(r"(\d+)%", output)
                    if match:
                        return int(match.group(1)) / 100.0

            # Fallback: use pactl to list sink inputs for activity
            result = subprocess.run(
                ["pactl", "list", "source-outputs", "short"],
                capture_output=True,
                text=True,
                timeout=1,
            )
            
            # If there are active source outputs, assume audio is present
            if result.returncode == 0 and result.stdout.strip():
                return 0.5  # Assume moderate audio level

        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            # pactl not available
            logger.debug("pactl not found, audio silence detection unavailable")
        except Exception as e:
            logger.debug(f"Audio level check error: {e}")

        return 0.0  # Default to silence

    def reset(self) -> None:
        super().reset()
        self._silence_start = None
        self._last_audio_level = 0.0


def create_default_detectors(config: DetectionConfig | None = None) -> list[DetectorBase]:
    """Create all default detectors with given config."""
    config = config or DetectionConfig()
    detectors = [
        TextIndicatorDetector(config),
        VideoElementDetector(config),
        URLChangeDetector(config),
        WebRTCConnectionDetector(config),
        ScreenFreezeDetector(config),
    ]
    
    # Add AudioSilenceDetector if enabled
    if config.audio_silence_enabled:
        detectors.append(AudioSilenceDetector(config))
    
    return detectors
