"""Tests for YouTube API route upload cleanup behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.routes.youtube as youtube_routes
import database.session as session_module
from database.migrations import run_schema_migrations
from database.models import Base, JobStatus, RecordingJob
from services.storage_maintenance import CanonicalRecording
from uploading.youtube import UploadResult, UploadStatus


@pytest.fixture
def youtube_route_session_local(tmp_path, monkeypatch):
    db_path = tmp_path / "youtube-route.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(session_module, "get_session_local", lambda: SessionLocal)
    return SessionLocal


def _create_youtube_job(SessionLocal, *, raw_path, trimmed_path):
    session = SessionLocal()
    try:
        session.add(
            RecordingJob(
                job_id="manual-trim",
                provider="jitsi",
                meeting_code="room",
                display_name="Recorder",
                duration_sec=120,
                status=JobStatus.SUCCEEDED.value,
                output_path=str(trimmed_path),
                raw_output_path=str(raw_path),
                trimmed_output_path=str(trimmed_path),
            )
        )
        session.commit()
    finally:
        session.close()


def _get_youtube_job(SessionLocal):
    session = SessionLocal()
    try:
        return session.query(RecordingJob).filter(RecordingJob.job_id == "manual-trim").first()
    finally:
        session.close()


def test_youtube_route_does_not_keep_artifact_identity_wrapper():
    assert not hasattr(youtube_routes, "_same_recording_artifact")
    assert not hasattr(youtube_routes, "_delete_trimmed_upload_artifacts")


@pytest.mark.asyncio
async def test_manual_upload_deletes_trimmed_artifacts_after_mkv_canonicalization(
    youtube_route_session_local, monkeypatch, tmp_path
):
    raw_path = tmp_path / "recording.mkv"
    trimmed_mkv_path = tmp_path / "recording.trimmed.mkv"
    trimmed_mp4_path = tmp_path / "recording.trimmed.mp4"
    temporary_upload_path = tmp_path / "recording.trimmed.upload.mp4"
    raw_path.write_bytes(b"raw")
    trimmed_mkv_path.write_bytes(b"trimmed")
    trimmed_mp4_path.write_bytes(b"mp4")
    temporary_upload_path.write_bytes(b"upload")
    _create_youtube_job(youtube_route_session_local, raw_path=raw_path, trimmed_path=trimmed_mkv_path)

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
    prepare_upload = AsyncMock(
        return_value=CanonicalRecording(
            output_path=trimmed_mp4_path,
            file_size=trimmed_mp4_path.stat().st_size,
            upload_path=temporary_upload_path,
            temporary_upload_path=temporary_upload_path,
        )
    )
    monkeypatch.setattr(youtube_routes, "get_youtube_uploader", lambda: uploader)
    monkeypatch.setattr(youtube_routes, "prepare_upload_recording_file", prepare_upload)

    response = await youtube_routes.upload_video(
        youtube_routes.UploadRequest(job_id="manual-trim", title="Meeting", privacy="unlisted")
    )

    assert response["success"] is True
    uploader.upload_video.assert_awaited_once()
    assert uploader.upload_video.await_args.kwargs["video_path"] == temporary_upload_path
    assert raw_path.exists()
    assert not trimmed_mkv_path.exists()
    assert not trimmed_mp4_path.exists()
    assert not temporary_upload_path.exists()

    job = _get_youtube_job(youtube_route_session_local)
    assert job.output_path == str(raw_path)
    assert job.trimmed_output_path == str(trimmed_mp4_path)
    assert job.youtube_video_id == "yt123"
    assert job.youtube_uploaded_at is not None
