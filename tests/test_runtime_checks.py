"""Tests for recording runtime readiness checks."""

from unittest.mock import Mock

import recording.runtime_checks as runtime_checks


class TestRecordingRuntimeStatus:
    """Tests for /health recording runtime readiness semantics."""

    def test_ready_when_audio_server_available_without_precreated_virtual_sink(self, monkeypatch):
        """Per-job sinks are created dynamically, so health only requires the audio server."""
        monkeypatch.setattr(runtime_checks.shutil, "which", lambda name: "/usr/bin/tool")

        def fake_run(cmd, **_kwargs):
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PipeWire")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\talsa_output\tmodule-alsa-card.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(runtime_checks.subprocess, "run", fake_run)

        status = runtime_checks.get_recording_runtime_status()

        assert status["audio_server_ready"] is True
        assert status["virtual_sink_ready"] is False
        assert status["dynamic_sink_supported"] is True
        assert status["ready"] is True

    def test_ready_when_audio_stack_and_virtual_sink_exist(self, monkeypatch):
        """Ready should include compatibility virtual sink details when it exists."""
        monkeypatch.setattr(runtime_checks.shutil, "which", lambda name: "/usr/bin/tool")

        def fake_run(cmd, **_kwargs):
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PipeWire")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\tvirtual_speaker\tmodule-null-sink.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(runtime_checks.subprocess, "run", fake_run)

        status = runtime_checks.get_recording_runtime_status()

        assert status["virtual_sink_ready"] is True
        assert status["dynamic_sink_supported"] is True
        assert status["ready"] is True

    def test_virtual_sink_detection_uses_exact_name(self, monkeypatch):
        """A sink with a shared prefix should not satisfy virtual_speaker readiness."""
        monkeypatch.setattr(runtime_checks.shutil, "which", lambda name: "/usr/bin/tool")

        def fake_run(cmd, **_kwargs):
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PipeWire")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\tvirtual_speaker_backup\tmodule-null-sink.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(runtime_checks.subprocess, "run", fake_run)

        status = runtime_checks.get_recording_runtime_status()

        assert status["virtual_sink_ready"] is False
        assert status["ready"] is True
