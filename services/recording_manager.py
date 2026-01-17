"""Recording management service for thumbnails, cleanup, and disk monitoring."""

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from utils.timezone import utc_now

logger = logging.getLogger(__name__)


class RecordingManager:
    """Manages recordings: thumbnails, cleanup, and disk monitoring."""

    def __init__(self, recordings_dir: str = "recordings"):
        self.recordings_dir = Path(recordings_dir)
        self.thumbnails_dir = self.recordings_dir / "thumbnails"
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    async def generate_thumbnail(
        self,
        video_path: str | Path,
        output_path: str | Path | None = None,
        timestamp_sec: float = 30.0,
        width: int = 320,
        height: int = 180,
    ) -> str | None:
        """Generate thumbnail from video using FFmpeg.

        Args:
            video_path: Path to the video file
            output_path: Optional custom output path, defaults to thumbnails dir
            timestamp_sec: Time position to capture thumbnail
            width: Thumbnail width
            height: Thumbnail height

        Returns:
            Path to generated thumbnail or None if failed
        """
        video_path = Path(video_path)

        if not video_path.exists():
            logger.warning(f"Video not found: {video_path}")
            return None

        if output_path is None:
            output_path = self.thumbnails_dir / f"{video_path.stem}.jpg"
        else:
            output_path = Path(output_path)

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(timestamp_sec),
                "-i",
                str(video_path),
                "-vframes",
                "1",
                "-vf",
                f"scale={width}:{height}",
                "-q:v",
                "2",
                str(output_path),
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode == 0 and output_path.exists():
                logger.info(f"Generated thumbnail: {output_path}")
                return str(output_path)
            else:
                logger.warning(f"FFmpeg failed: {stderr.decode()[:200]}")
                return None

        except Exception as e:
            logger.error(f"Thumbnail generation failed: {e}")
            return None

    async def cleanup_old_recordings(
        self,
        max_age_days: int = 30,
        max_count: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Clean up old recordings based on age or count.

        Args:
            max_age_days: Delete recordings older than this many days
            max_count: Keep only this many most recent recordings
            dry_run: If True, don't actually delete, just report

        Returns:
            Summary of cleanup operation
        """
        result = {
            "deleted_files": [],
            "deleted_count": 0,
            "freed_bytes": 0,
            "errors": [],
        }

        if not self.recordings_dir.exists():
            return result

        # Find all video files
        video_extensions = [".mkv", ".mp4", ".webm", ".avi"]
        videos = []
        for ext in video_extensions:
            videos.extend(self.recordings_dir.glob(f"*{ext}"))

        # Sort by modification time (newest first)
        videos.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        # Calculate cutoff date
        cutoff_date = utc_now() - timedelta(days=max_age_days)
        cutoff_timestamp = cutoff_date.timestamp()

        to_delete = []

        for i, video in enumerate(videos):
            should_delete = False
            reason = ""

            # Check age
            mtime = video.stat().st_mtime
            if mtime < cutoff_timestamp:
                should_delete = True
                reason = f"older than {max_age_days} days"

            # Check count limit
            if max_count is not None and i >= max_count:
                should_delete = True
                reason = f"exceeds max count of {max_count}"

            if should_delete:
                to_delete.append((video, reason))

        # Delete files
        for video, reason in to_delete:
            try:
                file_size = video.stat().st_size

                if not dry_run:
                    video.unlink()

                    # Also delete thumbnail if exists
                    thumbnail = self.thumbnails_dir / f"{video.stem}.jpg"
                    if thumbnail.exists():
                        thumbnail.unlink()

                result["deleted_files"].append(
                    {
                        "path": str(video),
                        "size": file_size,
                        "reason": reason,
                    }
                )
                result["deleted_count"] += 1
                result["freed_bytes"] += file_size

            except Exception as e:
                result["errors"].append(
                    {
                        "path": str(video),
                        "error": str(e),
                    }
                )

        if dry_run:
            logger.info(f"Dry run: would delete {result['deleted_count']} files")
        else:
            logger.info(f"Deleted {result['deleted_count']} files, freed {result['freed_bytes'] / 1024 / 1024:.1f} MB")

        return result

    def get_disk_usage(self) -> dict:
        """Get disk usage information for recordings directory.

        Returns:
            Dictionary with disk usage stats
        """
        if not self.recordings_dir.exists():
            return {
                "path": str(self.recordings_dir),
                "total_bytes": 0,
                "used_bytes": 0,
                "free_bytes": 0,
                "recordings_bytes": 0,
                "recordings_count": 0,
            }

        # Get filesystem stats
        disk_usage = shutil.disk_usage(self.recordings_dir)

        # Calculate recordings size
        recordings_bytes = 0
        recordings_count = 0
        video_extensions = [".mkv", ".mp4", ".webm", ".avi"]

        for ext in video_extensions:
            for video in self.recordings_dir.glob(f"*{ext}"):
                try:
                    recordings_bytes += video.stat().st_size
                    recordings_count += 1
                except OSError:
                    pass

        return {
            "path": str(self.recordings_dir),
            "total_bytes": disk_usage.total,
            "used_bytes": disk_usage.used,
            "free_bytes": disk_usage.free,
            "free_gb": disk_usage.free / (1024**3),
            "recordings_bytes": recordings_bytes,
            "recordings_gb": recordings_bytes / (1024**3),
            "recordings_count": recordings_count,
            "usage_percent": (disk_usage.used / disk_usage.total) * 100,
        }

    async def check_disk_space(
        self,
        threshold_gb: float = 10.0,
        auto_cleanup: bool = False,
        cleanup_target_gb: float = 20.0,
    ) -> dict:
        """Check disk space and optionally trigger cleanup.

        Args:
            threshold_gb: Warn if free space drops below this
            auto_cleanup: If True, automatically clean up when threshold is reached
            cleanup_target_gb: Target free space after cleanup

        Returns:
            Disk status and any cleanup performed
        """
        usage = self.get_disk_usage()

        result = {
            "status": "ok",
            "usage": usage,
            "cleanup_performed": False,
            "cleanup_result": None,
        }

        if usage["free_gb"] < threshold_gb:
            result["status"] = "low"
            logger.warning(f"Low disk space: {usage['free_gb']:.1f} GB remaining")

            if auto_cleanup:
                # Start with oldest recordings (more aggressive when disk is low)
                cleanup_result = await self.cleanup_old_recordings(
                    max_age_days=7,  # More aggressive when disk is low
                    dry_run=False,
                )

                result["cleanup_performed"] = True
                result["cleanup_result"] = cleanup_result

                # Check if we freed enough
                new_usage = self.get_disk_usage()
                if new_usage["free_gb"] >= threshold_gb:
                    result["status"] = "ok"

        return result

    def list_recordings(
        self,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "newest",
    ) -> list[dict]:
        """List recordings with metadata.

        Args:
            limit: Maximum number of recordings to return
            offset: Skip this many recordings
            order_by: Sort order ('newest', 'oldest', 'largest', 'smallest')

        Returns:
            List of recording info dictionaries
        """
        if not self.recordings_dir.exists():
            return []

        video_extensions = [".mkv", ".mp4", ".webm", ".avi"]
        videos = []

        for ext in video_extensions:
            videos.extend(self.recordings_dir.glob(f"*{ext}"))

        # Sort
        if order_by == "newest":
            videos.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        elif order_by == "oldest":
            videos.sort(key=lambda f: f.stat().st_mtime)
        elif order_by == "largest":
            videos.sort(key=lambda f: f.stat().st_size, reverse=True)
        elif order_by == "smallest":
            videos.sort(key=lambda f: f.stat().st_size)

        # Paginate
        videos = videos[offset : offset + limit]

        # Build result
        result = []
        for video in videos:
            stat = video.stat()
            thumbnail = self.thumbnails_dir / f"{video.stem}.jpg"

            result.append(
                {
                    "filename": video.name,
                    "path": str(video),
                    "size_bytes": stat.st_size,
                    "size_mb": stat.st_size / (1024 * 1024),
                    "created_at": datetime.fromtimestamp(stat.st_ctime, tz=UTC).isoformat(),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                    "has_thumbnail": thumbnail.exists(),
                    "thumbnail_path": str(thumbnail) if thumbnail.exists() else None,
                }
            )

        return result


# Global instance
_recording_manager: RecordingManager | None = None


def get_recording_manager(recordings_dir: str = "recordings") -> RecordingManager:
    """Get or create recording manager instance."""
    global _recording_manager
    if _recording_manager is None:
        _recording_manager = RecordingManager(recordings_dir)
    return _recording_manager
