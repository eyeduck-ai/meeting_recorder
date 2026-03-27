"""Contract tests for recording diagnostics output structure."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import recording.session as session_module
from providers.base import BaseProvider, MeetingState, MeetingStateSnapshot
from recording.session import RecordingSession


class FakeDiagnosticPage:
    """Minimal page stub used to exercise provider diagnostics."""

    def __init__(self):
        self.url = "https://example.test/meeting"
        self.viewport_size = {"width": 1280, "height": 720}

    async def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"fake-image")

    async def content(self) -> str:
        return "<html><body><div>fixture page</div></body></html>"

    async def title(self) -> str:
        return "Fixture Page"


class FakeDiagnosticProvider(BaseProvider):
    """Simple provider wrapper around the base diagnostic collector."""

    @property
    def name(self) -> str:
        return "fake"

    def build_join_url(self, meeting_code: str, base_url: str | None = None) -> str:
        return "https://example.test/meeting"

    async def prejoin(self, page, display_name: str, password: str | None = None) -> None:
        return None

    async def click_join(self, page) -> None:
        return None

    async def probe_state(self, page) -> MeetingStateSnapshot:
        return MeetingStateSnapshot(state=MeetingState.IN_MEETING)

    async def set_layout(self, page, preset: str = "speaker") -> bool:
        return True


def make_job(tmp_path: Path) -> SimpleNamespace:
    """Build a lightweight job object for RecordingSession tests."""
    return SimpleNamespace(
        job_id="diag1234",
        provider="jitsi",
        meeting_code="fixture-room",
        display_name="Recorder Bot",
        output_dir=tmp_path / "recordings" / "diag1234",
        attempt_no=1,
        base_url=None,
        password=None,
        lobby_wait_sec=900,
    )


@pytest.fixture
def patched_settings(monkeypatch, tmp_path):
    """Patch recording session settings to use temp directories."""
    settings = SimpleNamespace(
        diagnostics_dir=tmp_path / "diagnostics",
        recordings_dir=tmp_path / "recordings",
        resolution_w=1280,
        resolution_h=720,
    )
    monkeypatch.setattr(session_module, "get_settings", lambda: settings)
    return settings


class TestDiagnosticsContract:
    """Ensure diagnostics artifacts remain structurally consistent."""

    @pytest.mark.asyncio
    async def test_prepare_runtime_failure_writes_runtime_and_metadata(self, patched_settings, tmp_path):
        """Early failures should still emit runtime.json and metadata.json."""
        session = RecordingSession(make_job(tmp_path))

        runtime_summary = session.build_runtime_summary(
            failure_stage="prepare_runtime",
            error_code="VIRTUAL_ENV_ERROR",
            error_message="Xvfb missing",
        )
        diagnostic_data = await session.collect_diagnostics(
            error_code="VIRTUAL_ENV_ERROR",
            error_message="Xvfb missing",
            runtime_summary=runtime_summary,
        )

        assert session.runtime_path.exists()
        assert diagnostic_data.runtime_path == session.runtime_path
        assert diagnostic_data.metadata_path is not None and diagnostic_data.metadata_path.exists()
        assert diagnostic_data.provider_state_log_path is None
        metadata = diagnostic_data.metadata_path.read_text(encoding="utf-8")
        assert "prepare_runtime" in metadata

    @pytest.mark.asyncio
    async def test_join_failure_keeps_provider_state_log(self, patched_settings, tmp_path):
        """Join-stage failures should preserve provider_state.jsonl."""
        session = RecordingSession(make_job(tmp_path))
        session.record_provider_state(
            MeetingStateSnapshot(
                state=MeetingState.LOBBY,
                reason="Waiting for host",
                evidence={"matched_selectors": [".lobby-screen"]},
            ),
            "join_meeting",
        )

        runtime_summary = session.build_runtime_summary(
            failure_stage="join_meeting",
            error_code="LOBBY_TIMEOUT",
            error_message="Not admitted",
        )
        diagnostic_data = await session.collect_diagnostics(
            error_code="LOBBY_TIMEOUT",
            error_message="Not admitted",
            runtime_summary=runtime_summary,
        )

        assert diagnostic_data.provider_state_log_path is not None
        assert diagnostic_data.provider_state_log_path.exists()
        provider_state = diagnostic_data.provider_state_log_path.read_text(encoding="utf-8")
        assert '"stage": "join_meeting"' in provider_state
        assert '"attempt_no": 1' in provider_state

    @pytest.mark.asyncio
    async def test_monitor_failure_preserves_ffmpeg_log_and_page_artifacts(self, patched_settings, tmp_path):
        """Recording-stage failures should keep runtime, metadata, FFmpeg log, and provider state artifacts aligned."""
        session = RecordingSession(make_job(tmp_path))
        session.provider = FakeDiagnosticProvider()
        session.page = FakeDiagnosticPage()
        session._console_messages.append({"type": "log", "text": "smoke console", "timestamp": "2026-03-27T00:00:00"})

        session.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        (session.diagnostics_dir / "ffmpeg.log").write_text("ffmpeg output", encoding="utf-8")
        session.record_provider_state(
            MeetingStateSnapshot(
                state=MeetingState.ERROR,
                reason="FFmpeg exited early",
                error_code="FFMPEG_ERROR",
                error_message="ffmpeg exited",
            ),
            "monitor_recording",
        )

        runtime_summary = session.build_runtime_summary(
            failure_stage="monitor_recording",
            ffmpeg_exit_code=1,
            error_code="FFMPEG_ERROR",
            error_message="ffmpeg exited",
        )
        diagnostic_data = await session.collect_diagnostics(
            error_code="FFMPEG_ERROR",
            error_message="ffmpeg exited",
            runtime_summary=runtime_summary,
        )

        assert session.runtime_path.exists()
        assert (session.diagnostics_dir / "ffmpeg.log").exists()
        assert diagnostic_data.metadata_path is not None and diagnostic_data.metadata_path.exists()
        assert diagnostic_data.screenshot_path is not None and diagnostic_data.screenshot_path.exists()
        assert diagnostic_data.html_path is not None and diagnostic_data.html_path.exists()
        assert diagnostic_data.console_log_path is not None and diagnostic_data.console_log_path.exists()
        assert diagnostic_data.provider_state_log_path is not None and diagnostic_data.provider_state_log_path.exists()
