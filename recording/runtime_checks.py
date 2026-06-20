import shutil
import subprocess


def _pactl_short_names(output: str) -> set[str]:
    """Return exact device names from `pactl list ... short` output."""
    names = set()
    for line in output.splitlines():
        columns = line.split("\t")
        if len(columns) >= 2 and columns[1]:
            names.add(columns[1])
    return names


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
                virtual_sink_ready = "virtual_speaker" in _pactl_short_names(sinks.stdout)
        except Exception:
            audio_server_ready = False
            virtual_sink_ready = False

    ready = ffmpeg_available and xvfb_available and (not pactl_available or audio_server_ready)
    return {
        "ready": ready,
        "ffmpeg_available": ffmpeg_available,
        "xvfb_available": xvfb_available,
        "pactl_available": pactl_available,
        "audio_server_ready": audio_server_ready,
        "virtual_sink_ready": virtual_sink_ready,
        "dynamic_sink_supported": audio_server_ready,
    }
