"""Environment detection utilities."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EnvironmentStatus:
    """Environment status information."""

    is_linux: bool
    is_docker: bool
    is_recording_capable: bool
    platform: str
    warning_message: str | None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_linux": self.is_linux,
            "is_docker": self.is_docker,
            "is_recording_capable": self.is_recording_capable,
            "platform": self.platform,
            "warning_message": self.warning_message,
        }


def detect_environment() -> EnvironmentStatus:
    """Detect current runtime environment.

    Recording requires Linux (Xvfb + PipeWire dependencies).
    Windows/macOS users must use Docker.

    Returns:
        EnvironmentStatus with environment details and recording capability.
    """
    platform = sys.platform
    is_linux = platform.startswith("linux")
    is_docker = _is_running_in_docker()
    is_recording_capable = is_linux or is_docker

    warning_message = None
    if not is_recording_capable:
        platform_name = _get_platform_display_name(platform)
        warning_message = (
            f"Recording is not supported on {platform_name}. "
            "Xvfb + PipeWire dependencies require Linux. "
            "Please use Docker for recording functionality."
        )

    return EnvironmentStatus(
        is_linux=is_linux,
        is_docker=is_docker,
        is_recording_capable=is_recording_capable,
        platform=platform,
        warning_message=warning_message,
    )


def _is_running_in_docker() -> bool:
    """Check if running inside a Docker container."""
    # Method 1: Check for .dockerenv file
    if Path("/.dockerenv").exists():
        return True

    # Method 2: Check cgroup (Linux containers)
    try:
        cgroup_path = Path("/proc/1/cgroup")
        if cgroup_path.exists():
            content = cgroup_path.read_text()
            if "docker" in content or "kubepods" in content or "containerd" in content:
                return True
    except Exception:
        pass

    # Method 3: Check environment variable (can be set in Dockerfile)
    if os.environ.get("CONTAINER_ENV") == "docker":
        return True

    # Method 4: Check for typical container runtime socket
    if Path("/run/.containerenv").exists():
        return True

    return False


def _get_platform_display_name(platform: str) -> str:
    """Get human-readable platform name."""
    if platform.startswith("win"):
        return "Windows"
    elif platform == "darwin":
        return "macOS"
    elif platform.startswith("linux"):
        return "Linux"
    else:
        return platform


# Cached result
_cached_env_status: EnvironmentStatus | None = None


def get_environment_status() -> EnvironmentStatus:
    """Get cached environment status."""
    global _cached_env_status
    if _cached_env_status is None:
        _cached_env_status = detect_environment()
    return _cached_env_status
