"""Tests for database repository module."""

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from database.session import JobRepository, build_result_update_fields


class TestJobRepository:
    """Tests for JobRepository class."""

    def test_create_job(self):
        """Should create a job and add to session."""
        mock_session = Mock()
        repo = JobRepository(mock_session)

        with patch("database.models.RecordingJob") as MockJob:
            mock_job = Mock()
            MockJob.return_value = mock_job

            repo.create(
                job_id="abc12345",
                provider="jitsi",
                meeting_code="test-room",
                display_name="Bot",
                duration_sec=60,
                status="queued",
            )

        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()

    def test_get_by_job_id_found(self):
        """Should return job when found."""
        mock_session = Mock()
        mock_job = Mock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_job

        repo = JobRepository(mock_session)
        result = repo.get_by_job_id("abc12345")

        assert result == mock_job
        mock_session.query.assert_called_once()

    def test_get_by_job_id_not_found(self):
        """Should return None when job not found."""
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.first.return_value = None

        repo = JobRepository(mock_session)
        result = repo.get_by_job_id("nonexistent")

        assert result is None

    def test_get_all_with_pagination(self):
        """Should return jobs with pagination."""
        mock_session = Mock()
        mock_jobs = [Mock(), Mock(), Mock()]
        mock_query = mock_session.query.return_value
        mock_query.order_by.return_value.offset.return_value.limit.return_value.all.return_value = mock_jobs

        repo = JobRepository(mock_session)
        result = repo.get_all(limit=10, offset=5)

        assert result == mock_jobs
        mock_query.order_by.return_value.offset.assert_called_with(5)
        mock_query.order_by.return_value.offset.return_value.limit.assert_called_with(10)

    def test_get_by_status(self):
        """Should return jobs filtered by status."""
        mock_session = Mock()
        mock_jobs = [Mock(), Mock()]
        mock_session.query.return_value.filter.return_value.all.return_value = mock_jobs

        repo = JobRepository(mock_session)
        result = repo.get_by_status("recording")

        assert result == mock_jobs
        mock_session.query.assert_called_once()

    def test_update_status_success(self):
        """Should update status and return True."""
        mock_session = Mock()
        mock_job = Mock()
        mock_job.status = "queued"
        mock_session.query.return_value.filter.return_value.first.return_value = mock_job

        repo = JobRepository(mock_session)
        result = repo.update_status("abc12345", "recording")

        assert result is True
        assert mock_job.status == "recording"
        mock_session.flush.assert_called_once()

    def test_update_status_with_extra_fields(self):
        """Should update status and additional fields."""
        mock_session = Mock()
        mock_job = Mock()
        mock_job.status = "queued"
        mock_job.error_code = None
        mock_job.error_message = None
        mock_session.query.return_value.filter.return_value.first.return_value = mock_job

        repo = JobRepository(mock_session)
        result = repo.update_status(
            "abc12345", "failed", error_code="JOIN_TIMEOUT", error_message="Failed to join meeting"
        )

        assert result is True
        assert mock_job.status == "failed"
        assert mock_job.error_code == "JOIN_TIMEOUT"
        assert mock_job.error_message == "Failed to join meeting"

    def test_update_status_not_found(self):
        """Should return False when job not found."""
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.first.return_value = None

        repo = JobRepository(mock_session)
        result = repo.update_status("nonexistent", "failed")

        assert result is False

    def test_delete_success(self):
        """Should delete job and return True."""
        mock_session = Mock()
        mock_job = Mock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_job

        repo = JobRepository(mock_session)
        result = repo.delete("abc12345")

        assert result is True
        mock_session.delete.assert_called_once_with(mock_job)
        mock_session.flush.assert_called_once()

    def test_delete_not_found(self):
        """Should return False when job not found."""
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.first.return_value = None

        repo = JobRepository(mock_session)
        result = repo.delete("nonexistent")

        assert result is False
        mock_session.delete.assert_not_called()


class TestBuildResultUpdateFields:
    """Tests for build_result_update_fields function."""

    def test_basic_fields(self):
        """Should include basic fields from result."""
        result = Mock()
        result.end_time = datetime(2024, 1, 15, 12, 0, 0)
        result.error_code = None
        result.error_message = None
        result.joined_at = None
        result.recording_started_at = None
        result.recording_info = None
        result.diagnostic_data = None

        fields = build_result_update_fields(result)

        assert fields["completed_at"] == datetime(2024, 1, 15, 12, 0, 0)
        assert fields["error_code"] is None
        assert fields["error_message"] is None

    def test_with_error(self):
        """Should include error fields."""
        result = Mock()
        result.end_time = datetime(2024, 1, 15, 12, 0, 0)
        result.error_code = "JOIN_TIMEOUT"
        result.error_message = "Failed to join meeting within timeout"
        result.joined_at = None
        result.recording_started_at = None
        result.recording_info = None
        result.diagnostic_data = None

        fields = build_result_update_fields(result)

        assert fields["error_code"] == "JOIN_TIMEOUT"
        assert fields["error_message"] == "Failed to join meeting within timeout"

    def test_with_timing_fields(self):
        """Should include timing fields when present."""
        result = Mock()
        result.end_time = datetime(2024, 1, 15, 12, 0, 0)
        result.error_code = None
        result.error_message = None
        result.joined_at = datetime(2024, 1, 15, 11, 0, 5)
        result.recording_started_at = datetime(2024, 1, 15, 11, 0, 10)
        result.recording_info = None
        result.diagnostic_data = None

        fields = build_result_update_fields(result)

        assert fields["joined_at"] == datetime(2024, 1, 15, 11, 0, 5)
        assert fields["recording_started_at"] == datetime(2024, 1, 15, 11, 0, 10)

    def test_with_recording_info(self):
        """Should include recording info when present."""
        result = Mock()
        result.end_time = datetime(2024, 1, 15, 12, 0, 0)
        result.error_code = None
        result.error_message = None
        result.joined_at = None
        result.recording_started_at = None
        result.diagnostic_data = None

        # Mock recording info
        result.recording_info = Mock()
        result.recording_info.output_path = Path("/recordings/video.mp4")
        result.recording_info.file_size = 1024000
        result.recording_info.duration_sec = 3600

        fields = build_result_update_fields(result)

        # Check path contains expected parts (cross-platform)
        assert "video.mp4" in fields["output_path"]
        assert fields["file_size"] == 1024000
        assert fields["duration_actual_sec"] == 3600

    def test_with_diagnostic_data(self):
        """Should include diagnostic data when present."""
        result = Mock()
        result.end_time = datetime(2024, 1, 15, 12, 0, 0)
        result.error_code = "RECORDING_FAILED"
        result.error_message = "FFmpeg error"
        result.joined_at = None
        result.recording_started_at = None
        result.recording_info = None

        # Mock diagnostic data
        result.diagnostic_data = Mock()
        result.diagnostic_data.output_dir = Path("/diagnostics/job123")
        result.diagnostic_data.screenshot_path = Path("/diagnostics/job123/screenshot.png")
        result.diagnostic_data.html_path = Path("/diagnostics/job123/page.html")
        result.diagnostic_data.console_log_path = None

        fields = build_result_update_fields(result)

        assert "job123" in fields["diagnostic_dir"]
        assert fields["has_screenshot"] is True
        assert fields["has_html_dump"] is True
        assert fields["has_console_log"] is False

    def test_diagnostic_data_no_output_dir(self):
        """Should handle diagnostic data without output_dir."""
        result = Mock()
        result.end_time = datetime(2024, 1, 15, 12, 0, 0)
        result.error_code = None
        result.error_message = None
        result.joined_at = None
        result.recording_started_at = None
        result.recording_info = None

        # Mock diagnostic data with no output_dir
        result.diagnostic_data = Mock()
        result.diagnostic_data.output_dir = None
        result.diagnostic_data.screenshot_path = None
        result.diagnostic_data.html_path = None
        result.diagnostic_data.console_log_path = None

        fields = build_result_update_fields(result)

        assert fields["diagnostic_dir"] is None
        assert fields["has_screenshot"] is False
        assert fields["has_html_dump"] is False
        assert fields["has_console_log"] is False
