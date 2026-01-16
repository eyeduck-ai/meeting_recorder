"""YouTube test implementation."""

from config.settings import get_settings
from testing.models import TestResult
from testing.tests.base import BaseTest


class YouTubeTest(BaseTest):
    """Test YouTube API configuration and authorization."""

    name = "YouTube Test"
    description = "Check YouTube API status and optionally upload test video"

    def __init__(self, upload_test_video: bool = False) -> None:
        super().__init__()
        self.upload_test_video = upload_test_video

    async def run(self) -> TestResult:
        """Run YouTube test."""
        results = {}

        # Check configuration
        self.log("Checking YouTube configuration...")
        settings = get_settings()

        if not settings.youtube_client_id or not settings.youtube_client_secret:
            self.log("YouTube API credentials not configured", "ERROR")
            return TestResult(
                success=False,
                error="API credentials not configured",
                data={"configured": False},
            )

        results["configured"] = True
        self.log("YouTube API credentials configured")

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Check authorization status
        self.log("Checking authorization status...")
        try:
            from uploading.youtube import YouTubeUploader

            uploader = YouTubeUploader()

            if not uploader.is_authorized:
                self.log("YouTube not authorized", "WARNING")
                results["authorized"] = False
                return TestResult(
                    success=True,
                    data=results,
                )

            results["authorized"] = True
            self.log("YouTube is authorized")

        except Exception as e:
            self.log(f"Failed to check authorization: {e}", "ERROR")
            return TestResult(
                success=False,
                error=f"Authorization check failed: {e}",
                data=results,
            )

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Verify token validity
        self.log("Verifying token validity...")
        try:
            await uploader.ensure_valid_token()
            self.log("Token is valid", "SUCCESS")
            results["token_valid"] = True
        except Exception as e:
            self.log(f"Token validation failed: {e}", "ERROR")
            results["token_valid"] = False
            results["token_error"] = str(e)

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Upload test video if requested
        if self.upload_test_video and results.get("token_valid"):
            await self._upload_test_video(uploader, results)

        self.log("YouTube test completed", "SUCCESS")
        return TestResult(success=True, data=results)

    async def _upload_test_video(self, uploader, results: dict) -> None:
        """Upload a small test video."""
        self.log("Creating test video for upload...")

        try:
            import tempfile
            from pathlib import Path

            # Create a minimal test video using FFmpeg
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                test_video_path = Path(f.name)

            # Generate a 5-second test video
            import subprocess

            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=5:size=320x240:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=5",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                str(test_video_path),
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                self.log("Failed to create test video", "ERROR")
                results["upload_error"] = "Failed to create test video"
                return

            self.log(f"Test video created: {test_video_path}")

            # Upload the video
            self.log("Uploading test video to YouTube...")
            from uploading.youtube import VideoMetadata

            metadata = VideoMetadata(
                title="Meeting Recorder Test Upload",
                description="This is a test upload from Meeting Recorder. Safe to delete.",
                privacy_status="private",
            )

            video_id = await uploader.upload_video(
                video_path=test_video_path,
                metadata=metadata,
            )

            self.log(f"Test video uploaded: {video_id}", "SUCCESS")
            results["uploaded_video_id"] = video_id

            # Clean up
            test_video_path.unlink(missing_ok=True)

        except Exception as e:
            self.log(f"Upload failed: {e}", "ERROR")
            results["upload_error"] = str(e)
