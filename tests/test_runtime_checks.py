"""Tests for recording runtime readiness checks."""

from unittest.mock import Mock

import recording.runtime_checks as runtime_checks


class TestRecordingRuntimeStatus:
    """Tests for /health recording runtime readiness semantics."""

    def test_requires_virtual_sink_when_pactl_available(self, monkeypatch):
        """Ready should remain false when audio server is up but the virtual sink is missing."""
        monkeypatch.setattr(runtime_checks.shutil, "which", lambda name: "/usr/bin/tool")

        def fake_run(cmd, **kwargs):
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PipeWire")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\talsa_output\tmodule-alsa-card.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(runtime_checks.subprocess, "run", fake_run)

        status = runtime_checks.get_recording_runtime_status()

        assert status["audio_server_ready"] is True
        assert status["virtual_sink_ready"] is False
        assert status["ready"] is False

    def test_ready_when_audio_stack_and_virtual_sink_exist(self, monkeypatch):
        """Ready should become true only when ffmpeg, xvfb, pactl, server, and sink all exist."""
        monkeypatch.setattr(runtime_checks.shutil, "which", lambda name: "/usr/bin/tool")

        def fake_run(cmd, **kwargs):
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PipeWire")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\tvirtual_speaker\tmodule-null-sink.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(runtime_checks.subprocess, "run", fake_run)

        status = runtime_checks.get_recording_runtime_status()

        assert status["virtual_sink_ready"] is True
        assert status["ready"] is True
