"""Tests for recording module."""

import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

import recording.ffmpeg_pipeline as ffmpeg_module
import recording.session as session_module
import recording.worker as worker_module
from database.models import ErrorCode, JobStatus
from providers.base import JoinResult
from recording.activity import TrimDecision
from recording.ffmpeg_pipeline import FFmpegPipeline, RecordingInfo
from recording.session import RecordingSession
from recording.virtual_env import VirtualEnvironment, VirtualEnvironmentConfig
from recording.worker import RecordingJob, RecordingResult, RecordingWorker
from utils.timezone import utc_now


class TestRecordingJob:
    """Tests for RecordingJob dataclass."""

    def test_create_generates_unique_id(self):
        """Each job should have a unique ID."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job1 = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
            )
            job2 = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
            )

        assert job1.job_id != job2.job_id

    def test_create_id_format(self):
        """Job ID should be 8 characters (UUID prefix)."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
            )

        assert len(job.job_id) == 8
        # Should be valid hex
        int(job.job_id, 16)

    def test_create_output_dir_auto_generated(self):
        """Output dir should be auto-generated with timestamp and job_id."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
            )

        # Output dir should contain job_id
        assert job.job_id in str(job.output_dir)
        # Should be under recordings_dir (use 'in' for cross-platform)
        assert "recordings" in str(job.output_dir)
        # Should have timestamp format YYYYMMDD_HHMMSS
        dir_name = job.output_dir.name
        assert re.match(r"\d{8}_\d{6}_[a-f0-9]{8}", dir_name)

    def test_create_custom_output_dir(self):
        """Should use custom output_dir when provided."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900
        custom_dir = Path("/custom/output")

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
                output_dir=custom_dir,
            )

        assert job.output_dir == custom_dir

    def test_create_with_optional_params(self):
        """Should accept optional parameters."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="webex",
                meeting_code="https://webex.com/meet/user",
                display_name="Recorder",
                duration_sec=3600,
                base_url="https://company.webex.com",
                password="secret123",
                lobby_wait_sec=600,
            )

        assert job.provider == "webex"
        assert job.base_url == "https://company.webex.com"
        assert job.password == "secret123"
        assert job.lobby_wait_sec == 600

    def test_create_preserves_explicit_job_id_and_attempt(self):
        """Retry jobs should keep the same logical job_id and carry attempt metadata."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
                job_id="fixed1234",
                attempt_no=3,
            )

        assert job.job_id == "fixed1234"
        assert job.attempt_no == 3
        assert "fixed1234" in str(job.output_dir)

    def test_create_default_lobby_wait(self):
        """Should use settings default for lobby_wait_sec."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 1200  # Custom default

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
            )

        assert job.lobby_wait_sec == 1200

    def test_create_with_resolution_override(self):
        """Should keep explicit runtime resolution overrides on the job."""
        mock_settings = Mock()
        mock_settings.recordings_dir = Path("/recordings")
        mock_settings.lobby_wait_sec = 900

        with patch("recording.worker.get_settings", return_value=mock_settings):
            job = RecordingJob.create(
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
                resolution_w=1280,
                resolution_h=720,
            )

        assert job.resolution == (1280, 720)


class TestRecordingWorker:
    """Tests for RecordingWorker class."""

    def test_initial_state(self):
        """Worker should start with no job and QUEUED status."""
        worker = RecordingWorker()

        assert worker.is_busy is False
        assert worker.current_status == JobStatus.QUEUED

    def test_is_busy_when_job_running(self):
        """is_busy should return True when a job is set."""
        worker = RecordingWorker()
        worker._current_job = Mock()

        assert worker.is_busy is True

    def test_request_cancel_no_job(self):
        """request_cancel should return False when no job running."""
        worker = RecordingWorker()

        result = worker.request_cancel()

        assert result is False
        assert worker._cancel_requested is False

    def test_request_cancel_with_job(self):
        """request_cancel should set flag when job is running."""
        worker = RecordingWorker()
        worker._current_job = Mock()

        result = worker.request_cancel()

        assert result is True
        assert worker._cancel_requested is True

    def test_status_callback(self):
        """Status callback should be called on status update."""
        worker = RecordingWorker()
        callback = Mock()
        worker.set_status_callback(callback)

        # Simulate setting a job and updating status
        worker._current_job = Mock()
        worker._current_job.job_id = "test-123"
        worker._update_status(JobStatus.RECORDING)

        callback.assert_called_once_with("test-123", JobStatus.RECORDING)

    def test_status_callback_not_set(self):
        """Should not fail when callback is not set."""
        worker = RecordingWorker()
        worker._current_job = Mock()
        worker._current_job.job_id = "test-123"

        # Should not raise
        worker._update_status(JobStatus.RECORDING)

        assert worker._status == JobStatus.RECORDING

    def test_app_mode_failure_before_capture_builds_normal_auto_crop_fallback(self, tmp_path):
        """App-mode pre-capture failures should retry once in normal mode with crop protection."""
        worker = RecordingWorker()
        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path,
            recording_browser_mode="app",
            recording_crop_mode="off",
        )
        result = RecordingResult(job_id="job123", status=JobStatus.STARTING)

        assert worker._can_fallback_to_normal_browser(job, result) is True

        fallback_job = worker._build_normal_browser_fallback_job(job, "join_meeting: failed")

        assert fallback_job.recording_browser_mode == "app"
        assert fallback_job.resolved_browser_mode == "normal"
        assert fallback_job.recording_crop_mode == "auto"
        assert fallback_job.browser_fallback_used is True
        assert fallback_job.browser_fallback_reason == "join_meeting: failed"
        assert fallback_job.browser_fallback_attempts == 1

    def test_app_mode_failure_after_capture_start_does_not_fallback(self, tmp_path):
        """Once capture has started, failures should not relaunch browser fallback."""
        worker = RecordingWorker()
        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path,
            recording_browser_mode="app",
            recording_crop_mode="off",
        )
        result = RecordingResult(
            job_id="job123",
            status=JobStatus.RECORDING,
            recording_started_at=utc_now(),
        )

        assert worker._can_fallback_to_normal_browser(job, result) is False

    @pytest.mark.asyncio
    async def test_smart_trim_summary_records_expected_and_actual_duration(self, monkeypatch, tmp_path):
        raw_path = tmp_path / "recording.mkv"
        raw_path.write_bytes(b"raw")
        now = utc_now()
        raw_info = RecordingInfo(
            output_path=raw_path,
            file_size=raw_path.stat().st_size,
            duration_sec=30.0,
            start_time=now,
            end_time=now + timedelta(seconds=30),
        )
        session = SimpleNamespace(diagnostics_dir=tmp_path)
        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path,
        )
        result = RecordingResult(job_id="job123", status=JobStatus.FINALIZING, recording_info=raw_info)

        class FakeAnalyzer:
            def __init__(self, _config):
                pass

            async def analyze(self, _path):
                return TrimDecision(
                    status="trimmed",
                    reason="media activity boundaries detected",
                    trim_start_sec=2.0,
                    trim_end_sec=10.0,
                    duration_sec=30.0,
                    diagnostics={"probe": "fake"},
                )

        async def fake_trim_recording(**kwargs):
            trimmed_path = kwargs["output_path"]
            trimmed_path.write_bytes(b"trimmed")
            return RecordingInfo(
                output_path=trimmed_path,
                file_size=trimmed_path.stat().st_size,
                duration_sec=8.75,
                start_time=now,
                end_time=now + timedelta(seconds=8.75),
            )

        monkeypatch.setattr(worker_module, "RecordingActivityAnalyzer", FakeAnalyzer)
        monkeypatch.setattr(worker_module, "trim_recording", fake_trim_recording)

        await RecordingWorker()._apply_smart_trim(session, job, result)

        assert result.trim_diagnostics["trim_output_expected_duration_sec"] == 8.0
        assert result.trim_diagnostics["trim_output_actual_duration_sec"] == 8.75
        assert result.recording_info.duration_sec == 8.75

    @pytest.mark.asyncio
    async def test_record_retries_app_failure_once_with_normal_auto_crop(self, monkeypatch, tmp_path):
        """The worker should use an explicit second attempt for pre-capture app failures."""
        sessions = []

        class FakeSession:
            def __init__(self, job, runtime_resources=None):
                self.job = job
                self.runtime_resources = runtime_resources
                self.stage = None
                self.cleaned = False
                sessions.append(self)

            def begin_stage(self, stage):
                self.stage = stage

            def end_stage(self, stage, status="ok"):
                if self.stage == stage:
                    self.stage = None

            def current_stage(self):
                return self.stage

            async def prepare_runtime(self):
                if self.job.resolved_browser_mode in (None, "app"):
                    raise RuntimeError("app window failed")

            async def join_meeting(self):
                return JoinResult(success=True)

            async def dismiss_provider_overlays(self, _stage):
                return False

            async def set_layout(self, _preset):
                return True

            async def prepare_capture_surface(self):
                return None

            async def start_capture(self):
                return None

            async def probe_provider_state(self, _stage):
                return None

            async def finalize_capture(self):
                now = utc_now()
                return RecordingInfo(
                    output_path=self.job.output_dir / "recording.mkv",
                    file_size=123,
                    duration_sec=1.0,
                    start_time=now,
                    end_time=now,
                )

            def process_returncode(self):
                return None

            def build_runtime_summary(self, **_kwargs):
                return {
                    "resolved_browser_mode": self.job.resolved_browser_mode or self.job.recording_browser_mode,
                    "fallback_used": self.job.browser_fallback_used,
                    "fallback_reason": self.job.browser_fallback_reason,
                    "fallback_attempts": self.job.browser_fallback_attempts,
                    "crop_mode": self.job.recording_crop_mode,
                }

            async def collect_diagnostics(self, **_kwargs):
                return None

            async def cleanup(self):
                self.cleaned = True

        worker = RecordingWorker()
        monkeypatch.setattr("recording.worker.RecordingSession", FakeSession)
        monkeypatch.setattr(worker, "_monitor_recording", AsyncMock(return_value=("duration_elapsed", 0)))

        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path,
            recording_browser_mode="app",
            recording_crop_mode="off",
        )

        result = await worker.record(job)

        assert result.status == JobStatus.SUCCEEDED
        assert result.start_time is not None
        assert len(sessions) == 2
        assert sessions[0].cleaned is True
        assert sessions[1].cleaned is True
        assert sessions[1].job.resolved_browser_mode == "normal"
        assert sessions[1].job.recording_crop_mode == "auto"
        assert result.runtime_summary["resolved_browser_mode"] == "normal"
        assert result.runtime_summary["fallback_used"] is True
        assert result.runtime_summary["fallback_attempts"] == 1
        assert result.failure_stage is None
        assert worker.is_busy is False

    @pytest.mark.asyncio
    async def test_record_fails_before_runtime_when_disk_space_is_low(self, tmp_path):
        from recording.capacity_guard import RecordingCapacityGuard

        capacity_guard = RecordingCapacityGuard(
            settings_provider=lambda: SimpleNamespace(min_free_disk_gb_before_recording=10.0),
            disk_usage=lambda _path: SimpleNamespace(free=1024**3, total=20 * 1024**3, used=19 * 1024**3),
        )
        worker = RecordingWorker(capacity_guard=capacity_guard)

        job = RecordingJob(
            job_id="job-low-disk",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job-low-disk",
        )

        result = await worker.record(job)

        assert result.status == JobStatus.FAILED
        assert result.error_code == ErrorCode.DISK_FULL.value
        assert result.failure_stage == "prepare_runtime"
        assert worker.is_busy is False

    @pytest.mark.asyncio
    async def test_recording_session_constructor_type_error_is_not_fallback_swallowed(self, monkeypatch, tmp_path):
        class BrokenSession:
            def __init__(self, job, runtime_resources=None):
                raise TypeError("constructor bug")

        worker = RecordingWorker()
        monkeypatch.setattr("recording.worker.RecordingSession", BrokenSession)

        job = RecordingJob(
            job_id="job-type-error",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job-type-error",
        )

        result = await worker.record(job)

        assert result.status == JobStatus.FAILED
        assert result.error_code == ErrorCode.INTERNAL_ERROR.value
        assert result.error_message == "constructor bug"
        assert result.failure_stage == "prepare_runtime"
        assert worker.is_busy is False


class TestFFmpegPipeline:
    """Tests for FFmpeg command construction."""

    @pytest.fixture
    def ffmpeg_settings(self):
        return SimpleNamespace(
            ffmpeg_debug_ts=False,
            ffmpeg_thread_queue_size=1024,
            ffmpeg_preset="ultrafast",
            ffmpeg_crf=23,
            ffmpeg_audio_filter="aresample=async=1000:first_pts=0",
            ffmpeg_audio_bitrate="128k",
        )

    def _video_input(self, cmd: list[str]) -> str:
        x11grab_index = cmd.index("x11grab")
        input_index = cmd.index("-i", x11grab_index)
        return cmd[input_index + 1]

    def test_default_capture_uses_display_without_offset(self, monkeypatch, tmp_path, ffmpeg_settings):
        """Default x11grab input should preserve the existing display format."""
        monkeypatch.setattr(ffmpeg_module, "get_settings", lambda: ffmpeg_settings)
        monkeypatch.setattr(ffmpeg_module, "_check_pulseaudio_available", lambda _source: False)

        pipeline = FFmpegPipeline(
            output_path=tmp_path / "recording.mkv",
            display=":99",
            width=1280,
            height=720,
        )

        cmd = pipeline._build_command()

        assert self._video_input(cmd) == ":99"
        assert "1280x720" in cmd

    def test_offset_capture_uses_screen_coordinate_without_scaling(self, monkeypatch, tmp_path, ffmpeg_settings):
        """Top crop should offset x11grab while preserving configured output dimensions."""
        monkeypatch.setattr(ffmpeg_module, "get_settings", lambda: ffmpeg_settings)
        monkeypatch.setattr(ffmpeg_module, "_check_pulseaudio_available", lambda _source: False)

        pipeline = FFmpegPipeline(
            output_path=tmp_path / "recording.mkv",
            display=":99",
            width=1280,
            height=720,
            capture_y=72,
        )

        cmd = pipeline._build_command()

        assert self._video_input(cmd) == ":99.0+0,72"
        assert "1280x720" in cmd

    def test_gop_matches_one_second_keyframe_interval(self, monkeypatch, tmp_path, ffmpeg_settings):
        """Stream-copy smart trim should have roughly one-second keyframe boundaries."""
        monkeypatch.setattr(ffmpeg_module, "get_settings", lambda: ffmpeg_settings)
        monkeypatch.setattr(ffmpeg_module, "_check_pulseaudio_available", lambda _source: False)

        pipeline = FFmpegPipeline(
            output_path=tmp_path / "recording.mkv",
            display=":99",
            width=1280,
            height=720,
            framerate=30,
        )

        cmd = pipeline._build_command()

        assert cmd[cmd.index("-g") + 1] == "30"

    def test_pulseaudio_check_requires_exact_source_name(self, monkeypatch):
        """A monitor source should not match another source with the same prefix."""

        def fake_run(cmd, **_kwargs):
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0)
            if cmd == ["pactl", "list", "sources", "short"]:
                return Mock(returncode=0, stdout=b"0\tmr_sink_job12.monitor\tmodule-null-sink.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_run)

        assert ffmpeg_module._check_pulseaudio_available("mr_sink_job1.monitor") is False


class TestRecordingSession:
    """Tests for recording session runtime configuration."""

    def _fake_provider(self, join_url: str = "https://meet.test/room"):
        return SimpleNamespace(build_join_url=lambda _code, _base_url=None: join_url)

    @pytest.mark.asyncio
    async def test_dismiss_provider_overlays_delegates_to_provider(self):
        """RecordingSession should keep provider-specific overlay logic inside the provider."""
        page = object()
        provider = Mock()
        provider.dismiss_transient_overlays = AsyncMock(return_value=True)
        provider.probe_state = AsyncMock(return_value=Mock())

        session = RecordingSession.__new__(RecordingSession)
        session.page = page
        session.provider = provider
        session.record_provider_state = Mock()

        dismissed = await session.dismiss_provider_overlays("dismiss_overlays_pre_capture")

        assert dismissed is True
        provider.dismiss_transient_overlays.assert_awaited_once_with(page)
        provider.probe_state.assert_awaited_once_with(page)
        session.record_provider_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_job_resolution_for_runtime_and_capture(self, monkeypatch, tmp_path):
        """Virtual display, browser viewport, and FFmpeg should use job resolution."""
        captured = {}

        class FakeVirtualEnvironment:
            def __init__(self, config):
                captured["virtual_env_config"] = config
                self.display = ":99"
                self.pulse_monitor = "virtual.monitor"

            async def start(self):
                return {"DISPLAY": self.display}

        class FakePage:
            async def add_init_script(self, _script):
                return None

            def on(self, _event, _callback):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowser:
            async def new_context(self, *, viewport, permissions):
                captured["viewport"] = viewport
                captured["permissions"] = permissions
                return FakeContext()

        class FakeChromium:
            async def launch(self, *, headless, args, env):
                captured["launch_args"] = args
                captured["launch_env"] = env
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightManager:
            async def start(self):
                return FakePlaywright()

        class FakeFFmpegPipeline:
            def __init__(self, **kwargs):
                captured["ffmpeg_kwargs"] = kwargs
                self.is_recording = False

            async def start(self):
                captured["ffmpeg_started"] = True

        settings = Mock()
        settings.diagnostics_dir = tmp_path / "diagnostics"
        settings.resolution_w = 1920
        settings.resolution_h = 1080
        settings.recording_browser_mode = "normal"
        settings.recording_crop_mode = "off"
        settings.recording_crop_top_px = 0

        monkeypatch.setattr(session_module, "get_settings", lambda: settings)
        monkeypatch.setattr(session_module, "get_provider", lambda _provider: self._fake_provider())
        monkeypatch.setattr(session_module, "VirtualEnvironment", FakeVirtualEnvironment)
        monkeypatch.setattr(session_module, "async_playwright", lambda: FakePlaywrightManager())
        monkeypatch.setattr(session_module, "FFmpegPipeline", FakeFFmpegPipeline)

        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job123",
            resolution_w=1366,
            resolution_h=768,
            recording_browser_mode="normal",
            recording_crop_mode="off",
            diagnostics_dir=tmp_path / "diagnostics" / "job123",
        )
        session = RecordingSession(job)

        await session.prepare_runtime()
        await session.start_capture()

        assert captured["virtual_env_config"].width == 1366
        assert captured["virtual_env_config"].height == 768
        assert "--window-size=1366,768" in captured["launch_args"]
        assert "--kiosk" not in captured["launch_args"]
        assert "--start-fullscreen" not in captured["launch_args"]
        assert "--start-maximized" not in captured["launch_args"]
        assert "--app=about:blank" not in captured["launch_args"]
        assert not any(arg.startswith("--app=") for arg in captured["launch_args"])
        assert captured["viewport"] == {"width": 1366, "height": 768}
        assert captured["ffmpeg_kwargs"]["width"] == 1366
        assert captured["ffmpeg_kwargs"]["height"] == 768
        assert captured["ffmpeg_kwargs"]["capture_y"] == 0
        assert captured["ffmpeg_started"] is True

    @pytest.mark.asyncio
    async def test_app_browser_mode_uses_persistent_context_initial_page(self, monkeypatch, tmp_path):
        """App browser mode should launch the join URL as an app window and use its first page."""
        captured = {"new_page_called": False, "goto_called": False, "evaluate_called": False}
        join_url = "https://meet.test/app-room?pwd=secret-token#frag"

        class FakeVirtualEnvironment:
            def __init__(self, config):
                captured["virtual_env_config"] = config
                self.display = ":99"
                self.pulse_monitor = "virtual.monitor"

            async def start(self):
                return {"DISPLAY": self.display}

        class FakePage:
            async def add_init_script(self, _script):
                return None

            def on(self, _event, _callback):
                return None

            async def goto(self, *_args, **_kwargs):
                captured["goto_called"] = True

            async def evaluate(self, _script):
                captured["evaluate_called"] = True
                return None

        app_page = FakePage()

        class FakeContext:
            pages = [app_page]

            async def new_page(self):
                captured["new_page_called"] = True
                return FakePage()

        class FakeChromium:
            async def launch_persistent_context(self, user_data_dir, *, headless, args, env, viewport, permissions):
                captured["user_data_dir"] = user_data_dir
                captured["headless"] = headless
                captured["launch_args"] = args
                captured["launch_env"] = env
                captured["viewport"] = viewport
                captured["permissions"] = permissions
                return FakeContext()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightManager:
            async def start(self):
                return FakePlaywright()

        async def prejoin(page, _display_name, _password):
            captured["prejoin_page"] = page

        async def click_join(page):
            captured["click_join_page"] = page

        async def wait_until_joined(page, **_kwargs):
            captured["wait_page"] = page
            return JoinResult(success=True)

        provider = SimpleNamespace(
            build_join_url=lambda _code, _base_url=None: join_url,
            prejoin=prejoin,
            click_join=click_join,
            wait_until_joined=wait_until_joined,
        )

        settings = Mock()
        settings.diagnostics_dir = tmp_path / "diagnostics"
        settings.resolution_w = 1920
        settings.resolution_h = 1080
        settings.recording_browser_mode = "app"
        settings.recording_crop_mode = "off"
        settings.recording_crop_top_px = 0

        monkeypatch.setattr(session_module, "get_settings", lambda: settings)
        monkeypatch.setattr(session_module, "get_provider", lambda _provider: provider)
        monkeypatch.setattr(session_module, "VirtualEnvironment", FakeVirtualEnvironment)
        monkeypatch.setattr(session_module, "async_playwright", lambda: FakePlaywrightManager())

        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job123",
            resolution_w=1280,
            resolution_h=720,
            recording_browser_mode="app",
            recording_crop_mode="off",
            diagnostics_dir=tmp_path / "diagnostics" / "job123",
        )
        session = RecordingSession(job)

        await session.prepare_runtime()
        join_result = await session.join_meeting()
        summary = session.build_runtime_summary(end_reason="completed")

        assert join_result.success is True
        assert captured["virtual_env_config"].height == 720
        assert f"--app={join_url}" in captured["launch_args"]
        assert captured["viewport"] == {"width": 1280, "height": 720}
        assert captured["permissions"] == ["microphone"]
        assert captured["new_page_called"] is False
        assert captured["goto_called"] is False
        assert captured["evaluate_called"] is False
        assert captured["prejoin_page"] is app_page
        assert captured["click_join_page"] is app_page
        assert captured["wait_page"] is app_page
        assert summary["recording_browser_mode"] == "app"
        assert summary["resolved_browser_mode"] == "app"
        assert summary["browser_context_type"] == "persistent_app"
        assert summary["app_launch_url"] == "https://meet.test/app-room"
        assert "secret-token" not in summary["app_launch_url"]

    @pytest.mark.asyncio
    async def test_app_browser_mode_waits_for_initial_page_when_context_starts_empty(self):
        """App browser mode should tolerate delayed initial page creation."""
        captured = {}
        app_page = object()

        class FakeContext:
            pages = []

            async def wait_for_event(self, event, *, timeout):
                captured["wait_for_event"] = (event, timeout)
                return app_page

        session = RecordingSession.__new__(RecordingSession)

        page = await session._wait_for_initial_app_page(FakeContext())

        assert page is app_page
        assert captured["wait_for_event"] == ("page", session_module.APP_INITIAL_PAGE_TIMEOUT_MS)

    @pytest.mark.asyncio
    async def test_normal_browser_join_navigates_to_join_url(self):
        """Normal browser mode should keep the existing explicit navigation step."""
        captured = {}
        join_url = "https://meet.test/normal-room"

        class FakePage:
            async def goto(self, url, *, wait_until):
                captured["goto"] = (url, wait_until)

            async def evaluate(self, script):
                captured["fullscreen_script"] = script
                return None

        async def prejoin(page, _display_name, _password):
            captured["prejoin_page"] = page

        async def click_join(page):
            captured["click_join_page"] = page

        async def wait_until_joined(page, **_kwargs):
            captured["wait_page"] = page
            return JoinResult(success=True)

        page = FakePage()
        provider = SimpleNamespace(
            build_join_url=lambda _code, _base_url=None: join_url,
            prejoin=prejoin,
            click_join=click_join,
            wait_until_joined=wait_until_joined,
        )
        session = RecordingSession.__new__(RecordingSession)
        session.page = page
        session.provider = provider
        session.job = SimpleNamespace(meeting_code="room", base_url=None, display_name="Bot", password=None)
        session.resolved_browser_mode = "normal"
        session._join_url = None
        session.record_provider_state = Mock()

        result = await session.join_meeting()

        assert result.success is True
        assert captured["goto"] == (join_url, "domcontentloaded")
        assert "requestFullscreen" in captured["fullscreen_script"]
        assert captured["prejoin_page"] is page
        assert captured["click_join_page"] is page
        assert captured["wait_page"] is page

    @pytest.mark.asyncio
    async def test_top_crop_expands_display_and_offsets_capture(self, monkeypatch, tmp_path):
        """Top crop should make the display taller while preserving output dimensions."""
        captured = {}

        class FakeVirtualEnvironment:
            def __init__(self, config):
                captured["virtual_env_config"] = config
                self.display = ":99"
                self.pulse_monitor = "virtual.monitor"

            async def start(self):
                return {"DISPLAY": self.display}

        class FakePage:
            async def add_init_script(self, _script):
                return None

            def on(self, _event, _callback):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowser:
            async def new_context(self, *, viewport, permissions):
                captured["viewport"] = viewport
                return FakeContext()

        class FakeChromium:
            async def launch(self, *, headless, args, env):
                captured["launch_args"] = args
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightManager:
            async def start(self):
                return FakePlaywright()

        class FakeFFmpegPipeline:
            def __init__(self, **kwargs):
                captured["ffmpeg_kwargs"] = kwargs
                self.is_recording = False

            async def start(self):
                captured["ffmpeg_started"] = True

        settings = Mock()
        settings.diagnostics_dir = tmp_path / "diagnostics"
        settings.resolution_w = 1920
        settings.resolution_h = 1080
        settings.recording_browser_mode = "normal"
        settings.recording_crop_mode = "manual"
        settings.recording_crop_top_px = 0

        monkeypatch.setattr(session_module, "get_settings", lambda: settings)
        monkeypatch.setattr(session_module, "get_provider", lambda _provider: self._fake_provider())
        monkeypatch.setattr(session_module, "VirtualEnvironment", FakeVirtualEnvironment)
        monkeypatch.setattr(session_module, "async_playwright", lambda: FakePlaywrightManager())
        monkeypatch.setattr(session_module, "FFmpegPipeline", FakeFFmpegPipeline)

        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job123",
            resolution_w=1366,
            resolution_h=768,
            recording_browser_mode="normal",
            recording_crop_mode="manual",
            recording_crop_top_px=80,
            diagnostics_dir=tmp_path / "diagnostics" / "job123",
        )
        session = RecordingSession(job)

        await session.prepare_runtime()
        await session.start_capture()

        assert captured["virtual_env_config"].width == 1366
        assert captured["virtual_env_config"].height == 848
        assert "--window-size=1366,848" in captured["launch_args"]
        assert captured["viewport"] == {"width": 1366, "height": 768}
        assert captured["ffmpeg_kwargs"]["width"] == 1366
        assert captured["ffmpeg_kwargs"]["height"] == 768
        assert captured["ffmpeg_kwargs"]["capture_y"] == 80
        assert captured["ffmpeg_started"] is True

    @pytest.mark.asyncio
    async def test_prepare_capture_surface_records_browser_dimensions(self):
        """Capture preparation should persist browser surface diagnostics."""

        class FakePage:
            async def bring_to_front(self):
                self.brought_to_front = True

            async def evaluate(self, script):
                assert "const fullscreenRequestAllowed = true" in script
                return {
                    "fullscreenRequestAllowed": True,
                    "fullscreenRequested": True,
                    "innerWidth": 1280,
                    "innerHeight": 720,
                    "outerWidth": 1280,
                    "outerHeight": 800,
                }

        session = RecordingSession.__new__(RecordingSession)
        session.page = FakePage()
        session._capture_surface = None
        session.recording_crop_mode = "auto"
        session.configured_crop_top_px = 0
        session.auto_crop_reserved_px = 160
        session.resolved_crop_top_px = 0
        session.auto_crop_source = "auto_pending"

        await session.prepare_capture_surface()

        assert session._capture_surface["innerWidth"] == 1280
        assert session._capture_surface["outerHeight"] == 800
        assert session._capture_surface["fullscreenRequestAllowed"] is True
        assert session.resolved_crop_top_px == 83

    @pytest.mark.asyncio
    async def test_prepare_capture_surface_does_not_request_fullscreen_in_app_mode(self):
        """App browser mode should not trigger provider/browser fullscreen side effects."""

        class FakePage:
            async def bring_to_front(self):
                return None

            async def evaluate(self, script):
                assert "const fullscreenRequestAllowed = false" in script
                return {
                    "fullscreenElement": False,
                    "fullscreenRequestAllowed": False,
                    "fullscreenRequested": False,
                    "innerWidth": 1280,
                    "innerHeight": 720,
                    "outerWidth": 1280,
                    "outerHeight": 720,
                }

        session = RecordingSession.__new__(RecordingSession)
        session.page = FakePage()
        session.resolved_browser_mode = "app"
        session._capture_surface = None
        session.recording_crop_mode = "off"
        session.configured_crop_top_px = 0
        session.auto_crop_reserved_px = 0
        session.resolved_crop_top_px = 0
        session.auto_crop_source = "off"

        await session.prepare_capture_surface()

        assert session._capture_surface["fullscreenRequestAllowed"] is False
        assert session._capture_surface["fullscreenRequested"] is False

    @pytest.mark.asyncio
    async def test_auto_crop_reserves_display_and_uses_browser_dimensions(self, monkeypatch, tmp_path):
        """Auto crop should reserve display height and resolve capture offset from browser dimensions."""
        captured = {}

        class FakeVirtualEnvironment:
            def __init__(self, config):
                captured["virtual_env_config"] = config
                self.display = ":99"
                self.pulse_monitor = "virtual.monitor"

            async def start(self):
                return {"DISPLAY": self.display}

        class FakePage:
            async def add_init_script(self, _script):
                return None

            def on(self, _event, _callback):
                return None

            async def bring_to_front(self):
                return None

            async def evaluate(self, _script):
                return {
                    "innerWidth": 1280,
                    "innerHeight": 720,
                    "outerWidth": 1288,
                    "outerHeight": 805,
                    "screenHeight": 880,
                    "devicePixelRatio": 1,
                }

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowser:
            async def new_context(self, *, viewport, permissions):
                captured["viewport"] = viewport
                return FakeContext()

        class FakeChromium:
            async def launch(self, *, headless, args, env):
                captured["launch_args"] = args
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightManager:
            async def start(self):
                return FakePlaywright()

        class FakeFFmpegPipeline:
            def __init__(self, **kwargs):
                captured["ffmpeg_kwargs"] = kwargs
                self.is_recording = False

            async def start(self):
                captured["ffmpeg_started"] = True

        settings = Mock()
        settings.diagnostics_dir = tmp_path / "diagnostics"
        settings.resolution_w = 1920
        settings.resolution_h = 1080
        settings.recording_browser_mode = "normal"
        settings.recording_crop_mode = "auto"
        settings.recording_crop_top_px = 0

        monkeypatch.setattr(session_module, "get_settings", lambda: settings)
        monkeypatch.setattr(session_module, "get_provider", lambda _provider: self._fake_provider())
        monkeypatch.setattr(session_module, "VirtualEnvironment", FakeVirtualEnvironment)
        monkeypatch.setattr(session_module, "async_playwright", lambda: FakePlaywrightManager())
        monkeypatch.setattr(session_module, "FFmpegPipeline", FakeFFmpegPipeline)

        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job123",
            resolution_w=1280,
            resolution_h=720,
            recording_browser_mode="normal",
            recording_crop_mode="auto",
            recording_crop_top_px=0,
            diagnostics_dir=tmp_path / "diagnostics" / "job123",
        )
        session = RecordingSession(job)

        await session.prepare_runtime()
        await session.prepare_capture_surface()
        await session.start_capture()
        summary = session.build_runtime_summary(end_reason="completed")

        assert captured["virtual_env_config"].height == 880
        assert "--window-size=1280,880" in captured["launch_args"]
        assert captured["viewport"] == {"width": 1280, "height": 720}
        assert captured["ffmpeg_kwargs"]["capture_y"] == 88
        assert summary["crop_mode"] == "auto"
        assert summary["configured_crop_top_px"] == 0
        assert summary["resolved_crop_top_px"] == 88
        assert summary["auto_crop_source"] == "browser_outer_inner"
        assert summary["capture_frame"]["y"] == 88

    @pytest.mark.asyncio
    async def test_auto_crop_falls_back_when_browser_dimensions_are_missing(self, monkeypatch, tmp_path):
        """Auto crop should use configured fallback when browser dimensions are unavailable."""

        class FakePage:
            async def bring_to_front(self):
                return None

            async def evaluate(self, _script):
                return {"error": "no dimensions"}

        settings = Mock()
        settings.diagnostics_dir = tmp_path / "diagnostics"
        settings.resolution_w = 1280
        settings.resolution_h = 720
        settings.recording_crop_mode = "auto"
        settings.recording_crop_top_px = 0

        monkeypatch.setattr(session_module, "get_settings", lambda: settings)

        job = RecordingJob(
            job_id="job123",
            provider="jitsi",
            meeting_code="room",
            display_name="Bot",
            duration_sec=60,
            output_dir=tmp_path / "recordings" / "job123",
            resolution_w=1280,
            resolution_h=720,
            recording_crop_mode="auto",
            recording_crop_top_px=80,
            diagnostics_dir=tmp_path / "diagnostics" / "job123",
        )
        session = RecordingSession(job)
        session.page = FakePage()

        await session.prepare_capture_surface()

        assert session.resolved_crop_top_px == 80
        assert session.auto_crop_source == "fallback_configured"


class TestVirtualEnvironment:
    """Tests for virtual environment lifecycle behavior."""

    @pytest.mark.asyncio
    async def test_cleanup_xvfb_only_targets_owned_process_and_display_lock(self, monkeypatch):
        """Cleanup should stay scoped to the owned Xvfb process and the current display lock pid."""
        env = VirtualEnvironment(config=VirtualEnvironmentConfig(display_num=99))
        env._xvfb_process = Mock()
        env._xvfb_process.pid = 5678
        env._xvfb_process.poll.return_value = None

        terminate_owned = AsyncMock()
        terminate_stale = AsyncMock()
        cleanup_artifacts = Mock()

        monkeypatch.setattr(env, "_terminate_owned_process", terminate_owned)
        monkeypatch.setattr(env, "_terminate_stale_display_pid", terminate_stale)
        monkeypatch.setattr(env, "_cleanup_display_artifacts", cleanup_artifacts)
        monkeypatch.setattr(env, "_read_display_lock_pid", lambda: 1234)
        monkeypatch.setattr("recording.virtual_env.subprocess.run", Mock(side_effect=AssertionError("unexpected run")))

        await env._cleanup_xvfb()

        terminate_owned.assert_awaited_once()
        terminate_stale.assert_awaited_once_with(1234)
        cleanup_artifacts.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_setup_pulse_audio_verifies_sink_without_modifying_it(self, monkeypatch):
        """Audio setup should verify the sink and monitor source, then start keepalive."""
        env = VirtualEnvironment(config=VirtualEnvironmentConfig(pulse_sink_name="virtual_speaker"))
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(cmd)
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PulseAudio")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\tvirtual_speaker\tmodule-null-sink.c")
            if cmd == ["pactl", "list", "sources", "short"]:
                return Mock(returncode=0, stdout="0\tvirtual_speaker.monitor\tmodule-null-sink.c")
            raise AssertionError(f"Unexpected command: {cmd}")

        start_keepalive = AsyncMock()

        monkeypatch.setattr("recording.virtual_env.subprocess.run", fake_run)
        monkeypatch.setattr(env, "_start_audio_keepalive", start_keepalive)

        await env._setup_pulse_audio()

        start_keepalive.assert_awaited_once_with()
        assert ["pactl", "load-module", "module-null-sink"] not in [cmd[:3] for cmd in commands if len(cmd) >= 3]
        assert ["pactl", "set-default-sink", "virtual_speaker"] not in commands

    @pytest.mark.asyncio
    async def test_setup_pulse_audio_requires_exact_sink_match(self, monkeypatch):
        """A sink should not be treated as ready only because another sink shares its prefix."""
        env = VirtualEnvironment(config=VirtualEnvironmentConfig(pulse_sink_name="mr_sink_job1"))
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(cmd)
            if cmd == ["pactl", "info"]:
                return Mock(returncode=0, stdout="Server Name: PulseAudio")
            if cmd == ["pactl", "list", "sinks", "short"]:
                return Mock(returncode=0, stdout="0\tmr_sink_job12\tmodule-null-sink.c")
            if cmd[:3] == ["pactl", "load-module", "module-null-sink"]:
                return Mock(returncode=0, stdout="77\n", stderr="")
            if cmd == ["pactl", "list", "sources", "short"]:
                return Mock(returncode=0, stdout="")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr("recording.virtual_env.subprocess.run", fake_run)
        monkeypatch.setattr(env, "_start_audio_keepalive", AsyncMock())

        await env._setup_pulse_audio()

        assert any(cmd[:3] == ["pactl", "load-module", "module-null-sink"] for cmd in commands)

    @pytest.mark.asyncio
    async def test_stop_cleans_resources_even_when_not_marked_started(self, monkeypatch):
        """Startup cancellation after Xvfb begins should still allow cleanup."""
        env = VirtualEnvironment(config=VirtualEnvironmentConfig(display_num=99))
        env._xvfb_process = Mock()
        env._xvfb_process.poll.return_value = None
        env._audio_module_id = "77"
        terminate_owned = AsyncMock()
        cleanup_artifacts = Mock()
        unloaded = Mock()

        monkeypatch.setattr(env, "_terminate_owned_process", terminate_owned)
        monkeypatch.setattr(env, "_cleanup_display_artifacts", cleanup_artifacts)
        monkeypatch.setattr(env, "_unload_owned_audio_sink", unloaded)

        await env.stop()

        terminate_owned.assert_awaited_once()
        unloaded.assert_called_once_with()
        cleanup_artifacts.assert_called_once_with()
