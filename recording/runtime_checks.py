import shutil
import subprocess


def get_recording_runtime_status() -> dict:
    """Return readiness details for recording dependencies."""
    ffmpeg_available = shutil.which("ffmpeg") is not None
    xvfb_available = shutil.which("Xvfb") is not None
    pactl_available = shutil.which("pactl") is not None

    audio_server_ready = False
    virtual_sink_ready = False
    if pactl_available:
        try:
            info = subprocess.run(
                ["pactl", "info"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            audio_server_ready = info.returncode == 0
            if audio_server_ready:
                sinks = subprocess.run(
                    ["pactl", "list", "sinks", "short"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                virtual_sink_ready = "virtual_speaker" in sinks.stdout
        except Exception:
            audio_server_ready = False
            virtual_sink_ready = False

    ready = ffmpeg_available and xvfb_available and (not pactl_available or (audio_server_ready and virtual_sink_ready))
    return {
        "ready": ready,
        "ffmpeg_available": ffmpeg_available,
        "xvfb_available": xvfb_available,
        "pactl_available": pactl_available,
        "audio_server_ready": audio_server_ready,
        "virtual_sink_ready": virtual_sink_ready,
    }
