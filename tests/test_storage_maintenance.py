"""Tests for storage retention and local recording canonicalization."""

import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import services.storage_maintenance as storage_module
from database.migrations import run_schema_migrations
from database.models import Base, DetectionLog, JobStatus, RecordingJob
from services.storage_maintenance import StorageMaintenanceService
from utils.timezone import utc_now


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'storage.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def storage_settings(tmp_path):
    recordings_dir = tmp_path / "recordings"
    diagnostics_dir = tmp_path / "diagnostics"
    logs_dir = tmp_path / "logs"
    recordings_dir.mkdir()
    diagnostics_dir.mkdir()
    logs_dir.mkdir()
    return SimpleNamespace(
        recordings_dir=recordings_dir,
        diagnostics_dir=diagnostics_dir,
        logs_dir=logs_dir,
    )


def test_storage_maintenance_does_not_redefine_recording_variants():
    assert not hasattr(StorageMaintenanceService, "_recording_variants")


def _create_job(db_session, **kwargs) -> RecordingJob:
    job = RecordingJob(
        job_id=kwargs.pop("job_id", "job123"),
        provider="jitsi",
        meeting_code="room",
        display_name="Recorder",
        duration_sec=120,
        status=kwargs.pop("status", JobStatus.SUCCEEDED.value),
        created_at=kwargs.pop("created_at", utc_now()),
        **kwargs,
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.mark.asyncio
async def test_maintenance_canonicalizes_mkv_to_mp4_and_updates_job(db_session, storage_settings, monkeypatch):
    mkv_path = storage_settings.recordings_dir / "recording_job123.mkv"
    mp4_path = storage_settings.recordings_dir / "recording_job123.mp4"
    mkv_path.write_bytes(b"mkv-data")
    _create_job(db_session, output_path=str(mkv_path), file_size=mkv_path.stat().st_size)

    async def fake_ensure_canonical_mp4(input_path, **kwargs):
        mp4_path.write_bytes(b"mp4-data")
        return mp4_path

    monkeypatch.setattr(storage_module, "ensure_canonical_mp4", fake_ensure_canonical_mp4)

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    assert result["canonicalized"][0]["to"] == str(mp4_path)
    assert job.output_path == str(mp4_path)
    assert job.file_size == mp4_path.stat().st_size
    assert mp4_path.exists()
    assert not mkv_path.exists()


@pytest.mark.asyncio
async def test_maintenance_keeps_mkv_when_mp4_conversion_fails(db_session, storage_settings, monkeypatch):
    mkv_path = storage_settings.recordings_dir / "recording_job123.mkv"
    mkv_path.write_bytes(b"mkv-data")
    _create_job(db_session, output_path=str(mkv_path), file_size=mkv_path.stat().st_size)

    async def fake_ensure_canonical_mp4(input_path, **kwargs):
        return None

    monkeypatch.setattr(storage_module, "ensure_canonical_mp4", fake_ensure_canonical_mp4)

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    assert job.output_path == str(mkv_path)
    assert mkv_path.exists()
    assert result["canonicalized"] == []
    assert result["errors"]


@pytest.mark.asyncio
async def test_cleanup_uploaded_recordings_requires_youtube_and_age(db_session, storage_settings):
    old_time = utc_now() - timedelta(days=15)
    recent_time = utc_now() - timedelta(days=2)
    uploaded_old = storage_settings.recordings_dir / "uploaded_old.mp4"
    uploaded_recent = storage_settings.recordings_dir / "uploaded_recent.mp4"
    not_uploaded = storage_settings.recordings_dir / "not_uploaded.mp4"
    uploading = storage_settings.recordings_dir / "uploading.mp4"
    for path in (uploaded_old, uploaded_recent, not_uploaded, uploading):
        path.write_bytes(path.name.encode())

    _create_job(
        db_session,
        job_id="uploadedold",
        output_path=str(uploaded_old),
        file_size=uploaded_old.stat().st_size,
        completed_at=old_time,
        youtube_video_id="yt-old",
    )
    _create_job(
        db_session,
        job_id="uploadedrecent",
        output_path=str(uploaded_recent),
        file_size=uploaded_recent.stat().st_size,
        completed_at=recent_time,
        youtube_video_id="yt-new",
    )
    _create_job(
        db_session,
        job_id="notuploaded",
        output_path=str(not_uploaded),
        file_size=not_uploaded.stat().st_size,
        completed_at=old_time,
    )
    _create_job(
        db_session,
        job_id="uploading1",
        output_path=str(uploading),
        file_size=uploading.stat().st_size,
        completed_at=old_time,
        status=JobStatus.UPLOADING.value,
        youtube_video_id="yt-uploading",
    )

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    old_job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "uploadedold").first()
    assert len(result["deleted_recordings"]) == 1
    assert not uploaded_old.exists()
    assert old_job.local_recording_deleted_at is not None
    assert uploaded_recent.exists()
    assert not_uploaded.exists()
    assert uploading.exists()


@pytest.mark.asyncio
async def test_cleanup_uploaded_legacy_mkv_does_not_recanonicalize_deleted_source(db_session, storage_settings):
    old_time = utc_now() - timedelta(days=15)
    mkv_path = storage_settings.recordings_dir / "uploaded_old.mkv"
    mp4_path = storage_settings.recordings_dir / "uploaded_old.mp4"
    mkv_path.write_bytes(b"mkv-data")
    mp4_path.write_bytes(b"mp4-data")
    _create_job(
        db_session,
        output_path=str(mkv_path),
        file_size=mkv_path.stat().st_size,
        completed_at=old_time,
        youtube_video_id="yt-old",
    )

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    assert len(result["deleted_recordings"]) == 1
    assert result["canonicalized"] == []
    assert result["errors"] == []
    assert not mkv_path.exists()
    assert not mp4_path.exists()
    assert job.local_recording_deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_diagnostics_deletes_old_dirs_and_clears_job_flags(db_session, storage_settings):
    old_time = utc_now() - timedelta(days=15)
    diagnostic_dir = storage_settings.diagnostics_dir / "job123"
    diagnostic_dir.mkdir()
    (diagnostic_dir / "metadata.json").write_text("{}", encoding="utf-8")
    _create_job(
        db_session,
        output_path=str(storage_settings.recordings_dir / "recording.mp4"),
        completed_at=old_time,
        diagnostic_dir=str(diagnostic_dir),
        has_screenshot=True,
        has_html_dump=True,
        has_console_log=True,
    )

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    assert len(result["deleted_diagnostics"]) == 1
    assert not diagnostic_dir.exists()
    assert job.diagnostic_dir is None
    assert job.has_screenshot is False
    assert job.has_html_dump is False
    assert job.has_console_log is False


@pytest.mark.asyncio
async def test_cleanup_rotated_logs_keeps_current_app_log(db_session, storage_settings):
    old_log = storage_settings.logs_dir / "app.log.1"
    current_log = storage_settings.logs_dir / "app.log"
    keep_marker = storage_settings.logs_dir / ".gitkeep"
    old_log.write_text("old", encoding="utf-8")
    current_log.write_text("current", encoding="utf-8")
    keep_marker.write_text("", encoding="utf-8")
    old_timestamp = (utc_now() - timedelta(days=15)).timestamp()
    os.utime(old_log, (old_timestamp, old_timestamp))
    os.utime(current_log, (old_timestamp, old_timestamp))
    os.utime(keep_marker, (old_timestamp, old_timestamp))

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    assert len(result["deleted_logs"]) == 1
    assert not old_log.exists()
    assert current_log.exists()
    assert keep_marker.exists()


@pytest.mark.asyncio
async def test_cleanup_detection_logs_deletes_old_rows(db_session, storage_settings):
    job = _create_job(db_session)
    old_log = DetectionLog(
        job_id=job.id,
        detector_type="text",
        detected=True,
        triggered_at=utc_now() - timedelta(days=15),
    )
    recent_log = DetectionLog(
        job_id=job.id,
        detector_type="text",
        detected=False,
        triggered_at=utc_now() - timedelta(days=2),
    )
    db_session.add_all([old_log, recent_log])
    db_session.commit()

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=False)

    remaining = db_session.query(DetectionLog).all()
    assert result["deleted_detection_logs"] == 1
    assert [log.id for log in remaining] == [recent_log.id]


@pytest.mark.asyncio
async def test_dry_run_does_not_delete_or_update_db(db_session, storage_settings):
    old_time = utc_now() - timedelta(days=15)
    uploaded_old = storage_settings.recordings_dir / "uploaded_old.mp4"
    uploaded_old.write_bytes(b"video")
    _create_job(
        db_session,
        output_path=str(uploaded_old),
        file_size=uploaded_old.stat().st_size,
        completed_at=old_time,
        youtube_video_id="yt-old",
    )

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=True)

    job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    assert len(result["deleted_recordings"]) == 1
    assert uploaded_old.exists()
    assert job.local_recording_deleted_at is None


@pytest.mark.asyncio
async def test_dry_run_canonicalization_reports_would_attempt(db_session, storage_settings):
    mkv_path = storage_settings.recordings_dir / "recording_job123.mkv"
    mkv_path.write_bytes(b"mkv-data")
    _create_job(db_session, output_path=str(mkv_path), file_size=mkv_path.stat().st_size)

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=True)

    job = db_session.query(RecordingJob).filter(RecordingJob.job_id == "job123").first()
    assert result["canonicalized"][0]["status"] == "would_attempt"
    assert job.output_path == str(mkv_path)
    assert mkv_path.exists()


@pytest.mark.asyncio
async def test_maintenance_reports_missing_legacy_mkv_as_error(db_session, storage_settings):
    missing_path = storage_settings.recordings_dir / "missing.mkv"
    _create_job(db_session, output_path=str(missing_path), file_size=123)

    result = await StorageMaintenanceService(storage_settings).run(db_session, dry_run=True)

    assert result["canonicalized"] == []
    assert result["errors"][0]["scope"] == "canonicalize"
    assert "source MKV is missing" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_prepare_upload_uses_temporary_upload_transcode_path(storage_settings, monkeypatch):
    mkv_path = storage_settings.recordings_dir / "recording_job123.mkv"
    local_path = storage_settings.recordings_dir / "recording_job123.mp4"
    upload_path = storage_settings.recordings_dir / "recording_job123.upload.mp4"
    mkv_path.write_bytes(b"mkv-data")
    local_path.write_bytes(b"mp4-data")
    upload_path.write_bytes(b"upload-data")

    settings = SimpleNamespace(ffmpeg_transcode_on_upload=True)
    monkeypatch.setattr(storage_module, "get_settings", lambda: settings)
    monkeypatch.setattr(storage_module, "ensure_canonical_mp4", AsyncMock(return_value=local_path))
    monkeypatch.setattr(storage_module, "ensure_upload_mp4", AsyncMock(return_value=upload_path))

    result = await storage_module.prepare_upload_recording_file(mkv_path)

    assert result.output_path == local_path
    assert result.upload_path == upload_path
    assert result.temporary_upload_path == upload_path
    assert not mkv_path.exists()
    assert local_path.exists()
