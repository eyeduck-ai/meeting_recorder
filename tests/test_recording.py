"""Tests for recording module."""

import re
from pathlib import Path
from unittest.mock import Mock, patch

from database.models import JobStatus
from recording.worker import RecordingJob, RecordingWorker


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
