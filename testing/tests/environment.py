"""Environment check test implementation."""

import os
import shutil
import subprocess

from testing.models import TestResult
from testing.tests.base import BaseTest


class EnvironmentTest(BaseTest):
    """Test that checks system environment components."""

    name = "Environment Check"
    description = "Check Xvfb, PulseAudio, FFmpeg, Playwright, and disk space"

    async def run(self) -> TestResult:
        """Run environment checks."""
        results = {}
        all_ok = True

        # Check Xvfb
        self.log("Checking Xvfb...")
        xvfb_ok, xvfb_msg = self._check_xvfb()
        results["xvfb"] = {"ok": xvfb_ok, "message": xvfb_msg}
        self.log(f"  Xvfb: {'OK' if xvfb_ok else 'FAILED'} - {xvfb_msg}", "SUCCESS" if xvfb_ok else "ERROR")
        all_ok = all_ok and xvfb_ok

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Check PulseAudio
        self.log("Checking PulseAudio...")
        pulse_ok, pulse_msg = self._check_pulseaudio()
        results["pulseaudio"] = {"ok": pulse_ok, "message": pulse_msg}
        self.log(f"  PulseAudio: {'OK' if pulse_ok else 'FAILED'} - {pulse_msg}", "SUCCESS" if pulse_ok else "ERROR")
        all_ok = all_ok and pulse_ok

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Check FFmpeg
        self.log("Checking FFmpeg...")
        ffmpeg_ok, ffmpeg_msg = self._check_ffmpeg()
        results["ffmpeg"] = {"ok": ffmpeg_ok, "message": ffmpeg_msg}
        self.log(f"  FFmpeg: {'OK' if ffmpeg_ok else 'FAILED'} - {ffmpeg_msg}", "SUCCESS" if ffmpeg_ok else "ERROR")
        all_ok = all_ok and ffmpeg_ok

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Check Playwright/Chromium
        self.log("Checking Playwright/Chromium...")
        pw_ok, pw_msg = self._check_playwright()
        results["playwright"] = {"ok": pw_ok, "message": pw_msg}
        self.log(f"  Playwright: {'OK' if pw_ok else 'FAILED'} - {pw_msg}", "SUCCESS" if pw_ok else "ERROR")
        all_ok = all_ok and pw_ok

        if self.is_cancelled:
            return TestResult(success=False, error="Cancelled")

        # Check disk space
        self.log("Checking disk space...")
        disk_ok, disk_msg, disk_info = self._check_disk_space()
        results["disk"] = {"ok": disk_ok, "message": disk_msg, **disk_info}
        self.log(f"  Disk: {'OK' if disk_ok else 'WARNING'} - {disk_msg}", "SUCCESS" if disk_ok else "WARNING")

        # Check VNC info
        self.log("Checking VNC configuration...")
        vnc_info = self._get_vnc_info()
        results["vnc"] = vnc_info
        if vnc_info.get("enabled"):
            self.log(f"  VNC: Enabled on display {vnc_info.get('display')}", "SUCCESS")
        else:
            self.log("  VNC: Not enabled (set DEBUG_VNC=1 to enable)", "INFO")

        self.log("Environment check completed")
        return TestResult(success=all_ok, data=results)

    def _check_xvfb(self) -> tuple[bool, str]:
        """Check if Xvfb is available."""
        try:
            result = subprocess.run(
                ["which", "Xvfb"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Check if already running
                ps_result = subprocess.run(
                    ["pgrep", "-x", "Xvfb"],
                    capture_output=True,
                    text=True,
                )
                if ps_result.returncode == 0:
                    return True, "Installed and running"
                return True, "Installed (not running)"
            return False, "Not installed"
        except FileNotFoundError:
            return False, "which command not found (Windows?)"
        except subprocess.TimeoutExpired:
            return False, "Check timed out"
        except Exception as e:
            return False, str(e)

    def _check_pulseaudio(self) -> tuple[bool, str]:
        """Check if PulseAudio is available and configured."""
        try:
            result = subprocess.run(
                ["pactl", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False, "Not running"

            # Check for virtual sink
            sink_result = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "virtual_speaker" in sink_result.stdout:
                return True, "Running with virtual_speaker sink"
            return True, "Running (no virtual_speaker sink)"
        except FileNotFoundError:
            return False, "pactl not found"
        except subprocess.TimeoutExpired:
            return False, "Check timed out"
        except Exception as e:
            return False, str(e)

    def _check_ffmpeg(self) -> tuple[bool, str]:
        """Check if FFmpeg is available with required codecs."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False, "Not installed"

            version_line = result.stdout.split("\n")[0] if result.stdout else "Unknown version"

            # Check for x11grab (Linux only)
            encoders_result = subprocess.run(
                ["ffmpeg", "-encoders"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            has_x264 = "libx264" in encoders_result.stdout
            has_aac = "aac" in encoders_result.stdout

            if has_x264 and has_aac:
                return True, f"{version_line} (libx264, aac OK)"
            missing = []
            if not has_x264:
                missing.append("libx264")
            if not has_aac:
                missing.append("aac")
            return False, f"Missing encoders: {', '.join(missing)}"
        except FileNotFoundError:
            return False, "Not installed"
        except subprocess.TimeoutExpired:
            return False, "Check timed out"
        except Exception as e:
            return False, str(e)

    def _check_playwright(self) -> tuple[bool, str]:
        """Check if Playwright and Chromium are available."""
        try:
            from playwright._impl._driver import compute_driver_executable

            driver_path = compute_driver_executable()
            if os.path.exists(driver_path):
                return True, f"Installed (driver: {os.path.basename(driver_path)})"
            return False, "Driver not found"
        except ImportError:
            return False, "Playwright not installed"
        except Exception as e:
            return False, str(e)

    def _check_disk_space(self) -> tuple[bool, str, dict]:
        """Check available disk space."""
        try:
            recordings_dir = os.environ.get("RECORDINGS_DIR", "./recordings")
            if not os.path.exists(recordings_dir):
                recordings_dir = "."

            usage = shutil.disk_usage(recordings_dir)
            free_gb = usage.free / (1024**3)
            total_gb = usage.total / (1024**3)
            used_percent = (usage.used / usage.total) * 100

            info = {
                "free_gb": round(free_gb, 2),
                "total_gb": round(total_gb, 2),
                "used_percent": round(used_percent, 1),
            }

            if free_gb < 1:
                return False, f"Low disk space: {free_gb:.1f} GB free", info
            elif free_gb < 5:
                return True, f"Warning: {free_gb:.1f} GB free", info
            return True, f"{free_gb:.1f} GB free of {total_gb:.1f} GB", info
        except Exception as e:
            return False, str(e), {}

    def _get_vnc_info(self) -> dict:
        """Get VNC connection information."""
        debug_vnc = os.environ.get("DEBUG_VNC", "0") == "1"
        display = os.environ.get("DISPLAY", ":99")

        return {
            "enabled": debug_vnc,
            "display": display,
            "port": 5900,
            "instructions": "Connect using VNC client to container:5900" if debug_vnc else None,
        }
