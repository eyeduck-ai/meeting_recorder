"""Tests for scheduling upload orchestration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scheduling.upload_runner as upload_runner_module
from database.migrations import run_schema_migrations
from database.models import Base, JobStatus, RecordingJob
from scheduling.upload_runner import UploadRequest, YouTubeUploadRunner
from services.storage_maintenance import CanonicalRecording
from uploading.youtube import UploadResult, UploadStatus


@pytest.mark.asyncio
async def test_run_upload_task_remuxes_mkv_and_delegates_youtube_upload(tmp_path, monkeypatch):
    video_path = tmp_path / "recording.mkv"
    local_path = tmp_path / "recording.mp4"
    upload_path = tmp_path / "recording.upload.mp4"
    video_path.write_bytes(b"mkv")
    local_path.write_bytes(b"mp4")
    upload_path.write_bytes(b"upload")

    settings = SimpleNamespace(diagnostics_dir=tmp_path / "diagnostics")
    prepare_upload = AsyncMock(
        return_value=CanonicalRecording(
            output_path=local_path,
            file_size=local_path.stat().st_size,
            upload_path=upload_path,
            temporary_upload_path=upload_path,
        )
    )
    clear_progress = Mock()
    update_progress = Mock()

    monkeypatch.setattr(upload_runner_module, "get_settings", lambda: settings)
    monkeypatch.setattr(upload_runner_module, "prepare_upload_recording_file", prepare_upload)
    monkeypatch.setattr(upload_runner_module, "clear_progress", clear_progress)
    monkeypatch.setattr(upload_runner_module, "update_progress", update_progress)

    runner = YouTubeUploadRunner()
    runner.upload_to_youtube = AsyncMock(return_value=True)
    runner._persist_local_recording_path = Mock()

    await runner.run_upload_task(
        UploadRequest(
            job_id="job123",
            video_path=video_path,
            title="Meeting",
            privacy="unlisted",
        )
    )

    prepare_upload.assert_awaited_once()
    ensure_kwargs = prepare_upload.await_args.kwargs
    assert ensure_kwargs["remux_log_path"] == settings.diagnostics_dir / "job123" / "remux.log"
    assert ensure_kwargs["transcode_log_path"] == settings.diagnostics_dir / "job123" / "transcode.log"

    ensure_kwargs["progress_callback"](100, 200)
    update_progress.assert_called_with("job123", "compressing", 100, 200, "ms")
    runner.upload_to_youtube.assert_awaited_once_with(
        job_id="job123",
        video_path=upload_path,
        title="Meeting",
        privacy="unlisted",
    )
    runner._persist_local_recording_path.assert_called_once()
    assert not upload_path.exists()
    assert local_path.exists()
    clear_progress.assert_called_once_with("job123")


def _build_upload_session(tmp_path, monkeypatch, *, status=JobStatus.SUCCEEDED.value):
    engine = create_engine(f"sqlite:///{tmp_path / 'upload.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(upload_runner_module, "get_session_local", lambda: SessionLocal)
    return SessionLocal


def _create_upload_job(
    SessionLocal,
    video_path,
    *,
    status=JobStatus.SUCCEEDED.value,
    raw_output_path=None,
    trimmed_output_path=None,
):
    session = SessionLocal()
    try:
        session.add(
            RecordingJob(
                job_id="job123",
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder",
                duration_sec=120,
                status=status,
                output_path=str(video_path),
                raw_output_path=str(raw_output_path) if raw_output_path else None,
                trimmed_output_path=str(trimmed_output_path) if trimmed_output_path else None,
            )
        )
        session.commit()
    finally:
        session.close()


def _get_upload_job(SessionLocal):
    session = SessionLocal()
    try:
        return session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_upload_to_youtube_persists_video_id_and_uploaded_at(tmp_path, monkeypatch):
    SessionLocal = _build_upload_session(tmp_path, monkeypatch)
    monkeypatch.setattr(upload_runner_module, "update_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "notify_youtube_upload_completed", AsyncMock())

    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"mp4")
    _create_upload_job(SessionLocal, video_path)

    uploader = SimpleNamespace(
        is_configured=True,
        is_authorized=True,
        upload_video=AsyncMock(
            return_value=UploadResult(
                status=UploadStatus.SUCCEEDED,
                video_id="yt123",
                video_url="https://youtu.be/yt123",
            )
        ),
    )
    monkeypatch.setattr(upload_runner_module, "get_youtube_uploader", lambda: uploader)

    await YouTubeUploadRunner().upload_to_youtube(
        job_id="job123",
        video_path=video_path,
        title="Meeting",
        privacy="unlisted",
    )

    job = _get_upload_job(SessionLocal)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.youtube_video_id == "yt123"
    assert job.youtube_uploaded_at is not None


@pytest.mark.asyncio
async def test_upload_to_youtube_checks_auth_before_uploading_status(tmp_path, monkeypatch):
    SessionLocal = _build_upload_session(tmp_path, monkeypatch)
    monkeypatch.setattr(upload_runner_module, "update_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "clear_progress", Mock())
    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"mp4")
    _create_upload_job(SessionLocal, video_path)

    uploader = SimpleNamespace(is_configured=False, is_authorized=False, upload_video=AsyncMock())
    monkeypatch.setattr(upload_runner_module, "get_youtube_uploader", lambda: uploader)

    await YouTubeUploadRunner().upload_to_youtube(
        job_id="job123",
        video_path=video_path,
        title="Meeting",
        privacy="unlisted",
    )

    job = _get_upload_job(SessionLocal)
    assert job.status == JobStatus.SUCCEEDED.value
    uploader.upload_video.assert_not_awaited()
    upload_runner_module.update_progress.assert_not_called()
    upload_runner_module.clear_progress.assert_called_with("job123")


@pytest.mark.asyncio
async def test_upload_to_youtube_restores_success_on_upload_failure(tmp_path, monkeypatch):
    SessionLocal = _build_upload_session(tmp_path, monkeypatch)
    monkeypatch.setattr(upload_runner_module, "update_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "clear_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "notify_youtube_upload_completed", AsyncMock())
    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"mp4")
    _create_upload_job(SessionLocal, video_path)

    uploader = SimpleNamespace(
        is_configured=True,
        is_authorized=True,
        upload_video=AsyncMock(return_value=UploadResult(status=UploadStatus.FAILED, error_message="quota exceeded")),
    )
    monkeypatch.setattr(upload_runner_module, "get_youtube_uploader", lambda: uploader)

    await YouTubeUploadRunner().upload_to_youtube(
        job_id="job123",
        video_path=video_path,
        title="Meeting",
        privacy="unlisted",
    )

    job = _get_upload_job(SessionLocal)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.youtube_video_id is None
    upload_runner_module.clear_progress.assert_called_with("job123")


@pytest.mark.asyncio
async def test_upload_to_youtube_restores_success_on_exception(tmp_path, monkeypatch):
    SessionLocal = _build_upload_session(tmp_path, monkeypatch)
    monkeypatch.setattr(upload_runner_module, "update_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "clear_progress", Mock())
    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"mp4")
    _create_upload_job(SessionLocal, video_path)

    uploader = SimpleNamespace(
        is_configured=True,
        is_authorized=True,
        upload_video=AsyncMock(side_effect=RuntimeError("network error")),
    )
    monkeypatch.setattr(upload_runner_module, "get_youtube_uploader", lambda: uploader)

    await YouTubeUploadRunner().upload_to_youtube(
        job_id="job123",
        video_path=video_path,
        title="Meeting",
        privacy="unlisted",
    )

    job = _get_upload_job(SessionLocal)
    assert job.status == JobStatus.SUCCEEDED.value
    upload_runner_module.clear_progress.assert_called_with("job123")


@pytest.mark.asyncio
async def test_run_upload_task_cleans_trimmed_artifact_after_success(tmp_path, monkeypatch):
    raw_path = tmp_path / "recording.mkv"
    trimmed_path = tmp_path / "recording.trimmed.mkv"
    upload_path = tmp_path / "recording.trimmed.mp4"
    raw_path.write_bytes(b"raw")
    trimmed_path.write_bytes(b"trimmed")
    upload_path.write_bytes(b"mp4")

    prepare_upload = AsyncMock(
        return_value=CanonicalRecording(
            output_path=upload_path,
            file_size=upload_path.stat().st_size,
        )
    )
    monkeypatch.setattr(upload_runner_module, "get_settings", lambda: SimpleNamespace(diagnostics_dir=tmp_path))
    monkeypatch.setattr(upload_runner_module, "prepare_upload_recording_file", prepare_upload)
    monkeypatch.setattr(upload_runner_module, "clear_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "update_progress", Mock())

    runner = YouTubeUploadRunner()
    runner.upload_to_youtube = AsyncMock(return_value=True)
    runner._persist_local_recording_path = Mock()
    runner._cleanup_uploaded_trimmed_output = AsyncMock()

    await runner.run_upload_task(
        UploadRequest(
            job_id="job123",
            video_path=trimmed_path,
            title="Meeting",
            privacy="unlisted",
            raw_video_path=raw_path,
            cleanup_video_path_after_success=trimmed_path,
        )
    )

    runner._cleanup_uploaded_trimmed_output.assert_awaited_once_with(
        job_id="job123",
        cleanup_path=trimmed_path,
        prepared_upload_path=upload_path,
        raw_video_path=raw_path,
    )


@pytest.mark.asyncio
async def test_run_upload_task_deletes_canonical_trimmed_mp4_when_upload_transcode_uses_temp(tmp_path, monkeypatch):
    SessionLocal = _build_upload_session(tmp_path, monkeypatch)
    raw_path = tmp_path / "recording.mkv"
    trimmed_mkv_path = tmp_path / "recording.trimmed.mkv"
    trimmed_mp4_path = tmp_path / "recording.trimmed.mp4"
    temporary_upload_path = tmp_path / "recording.trimmed.upload.mp4"
    raw_path.write_bytes(b"raw")
    trimmed_mkv_path.write_bytes(b"trimmed")
    trimmed_mp4_path.write_bytes(b"mp4")
    temporary_upload_path.write_bytes(b"upload")
    _create_upload_job(
        SessionLocal,
        trimmed_mkv_path,
        raw_output_path=raw_path,
        trimmed_output_path=trimmed_mkv_path,
    )

    prepare_upload = AsyncMock(
        return_value=CanonicalRecording(
            output_path=trimmed_mp4_path,
            file_size=trimmed_mp4_path.stat().st_size,
            upload_path=temporary_upload_path,
            temporary_upload_path=temporary_upload_path,
        )
    )
    monkeypatch.setattr(upload_runner_module, "get_settings", lambda: SimpleNamespace(diagnostics_dir=tmp_path))
    monkeypatch.setattr(upload_runner_module, "prepare_upload_recording_file", prepare_upload)
    monkeypatch.setattr(upload_runner_module, "clear_progress", Mock())
    monkeypatch.setattr(upload_runner_module, "update_progress", Mock())

    runner = YouTubeUploadRunner()
    runner.upload_to_youtube = AsyncMock(return_value=True)

    await runner.run_upload_task(
        UploadRequest(
            job_id="job123",
            video_path=trimmed_mkv_path,
            title="Meeting",
            privacy="unlisted",
            raw_video_path=raw_path,
            cleanup_video_path_after_success=trimmed_mkv_path,
        )
    )

    runner.upload_to_youtube.assert_awaited_once_with(
        job_id="job123",
        video_path=temporary_upload_path,
        title="Meeting",
        privacy="unlisted",
    )
    assert raw_path.exists()
    assert not trimmed_mkv_path.exists()
    assert not trimmed_mp4_path.exists()
    assert not temporary_upload_path.exists()

    job = _get_upload_job(SessionLocal)
    assert job.output_path == str(raw_path)
