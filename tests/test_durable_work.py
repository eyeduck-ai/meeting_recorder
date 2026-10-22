"""Restart, deduplication and private resumable state regression coverage."""

import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scheduling.job_runner as runner_module
import scheduling.upload_runner as upload_module
from database.base import Base
from database.models import DurableWork, JobStatus
from database.models import RecordingJob as JobModel
from recording.ffmpeg_pipeline import RecordingInfo
from recording.job_types import RecordingJob, RecordingResult
from recording.post_processing import RecordingPostProcessingRequest
from scheduling.job_runner import JobRunner
from scheduling.upload_runner import UploadRequest, YouTubeUploadRunner
from services.durable_work import DurableWorkStore, post_processing_payload, upload_payload
from uploading.youtube import UploadStatus, VideoMetadata, YouTubeUploader
from utils.timezone import utc_now


def database(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    return engine, sessions, DurableWorkStore(session_factory=lambda: sessions)


def add_job(sessions, job_id="pending", **kwargs):
    with sessions() as session:
        session.add(
            JobModel(
                job_id=job_id,
                provider="jitsi",
                meeting_code="https://example.test/room?pwd=secret",
                display_name="Recorder",
                duration_sec=120,
                status=JobStatus.SUCCEEDED.value,
                **kwargs,
            )
        )
        session.commit()


def test_snapshot_and_postprocessing_payload_never_contain_meeting_credentials(tmp_path):
    engine, sessions, store = database(tmp_path / "work.db")
    add_job(sessions)
    media = tmp_path / "raw.mp4"
    media.write_bytes(b"recording")
    now = utc_now()
    job = RecordingJob(
        job_id="pending",
        provider="jitsi",
        meeting_code="https://room.test/?pwd=private",
        password="secret",
        base_url="https://room.test/?token=secret",
        display_name="Recorder",
        duration_sec=120,
        output_dir=tmp_path,
    )
    result = RecordingResult(
        job_id=job.job_id,
        status=JobStatus.SUCCEEDED,
        recording_info=RecordingInfo(media, 9, 120, now, now + timedelta(seconds=120)),
    )
    payload = post_processing_payload(RecordingPostProcessingRequest(job, result, True, "unlisted", "Meeting"))
    assert "secret" not in json.dumps(payload)
    assert "meeting_code" not in json.dumps(payload)
    store.ensure(job.job_id, "post_process", payload)
    assert store.claim(job.job_id, "post_process")
    assert not store.claim(job.job_id, "post_process")
    engine.dispose()
    _, reopened_sessions, reopened = database(tmp_path / "work.db")
    reopened.reset_interrupted()
    recovered = reopened.restore_request(*reopened.ready()[0])
    assert recovered.job.meeting_code == "https://example.test/room?pwd=secret"
    assert recovered.job.password is None
    assert recovered.result.recording_info.output_path == media
    upload = UploadRequest(job.job_id, media, "Recording", "unlisted")
    reopened.succeed(job.job_id, "post_process", upload_request=upload)
    reopened.save_resume_state(job.job_id, {"session_uri": "https://private.test/session?token=secret"})
    snapshot = json.dumps(reopened.snapshot())
    assert "session_uri" not in snapshot and "secret" not in snapshot
    assert reopened.snapshot()["counts"] == {"succeeded": 1, "pending": 1}
    with reopened_sessions() as session:
        assert session.query(DurableWork).count() == 2


def test_work_retries_have_bounded_backoff_and_manual_retry_keeps_session(tmp_path):
    _, _, store = database(tmp_path / "work.db")
    store.ensure("retry", "upload", {"job_id": "retry"})
    store.save_resume_state("retry", {"session_uri": "private"})
    for _attempt in range(5):
        with store._session() as session:
            row = session.query(DurableWork).one()
            row.next_attempt_at = None
            session.commit()
        assert store.claim("retry", "upload")
        store.fail("retry", "upload", error="Temporary transport error")
        assert not store.ready()
    assert store.snapshot()["counts"] == {"failed": 1}
    assert store.retry_failed("retry", "upload")
    assert store.resume_state("retry")["session_uri"] == "private"
    assert store.claim("retry", "upload")


@pytest.mark.asyncio
async def test_runner_restart_recovers_only_intents_and_never_replays_history(tmp_path, monkeypatch):
    path = tmp_path / "work.db"
    engine, sessions, store = database(path)
    media = tmp_path / "raw.mp4"
    media.write_bytes(b"raw")
    add_job(sessions, "historic", output_path=str(media), youtube_enabled=True)
    add_job(sessions, "pending", output_path=str(media), youtube_enabled=True)
    request = UploadRequest("pending", media, "Meeting", "unlisted")
    store.ensure("pending", "upload", upload_payload(request))
    assert store.claim("pending", "upload")
    engine.dispose()
    _, sessions, _ = database(path)
    monkeypatch.setattr(runner_module, "get_session_local", lambda: sessions)
    started = asyncio.Event()
    release = asyncio.Event()

    async def upload(_request):
        started.set()
        await release.wait()
        return True

    runner = JobRunner()
    runner._upload_runner.run_upload_task = AsyncMock(side_effect=upload)
    await runner.recover_durable_work()
    await asyncio.wait_for(started.wait(), 1)
    assert runner.active_count == 0
    runner.enqueue_upload(request)
    assert runner._upload_runner.run_upload_task.await_count == 1
    release.set()
    await asyncio.gather(*list(runner._upload_tasks))
    await runner.shutdown()
    assert runner.durable_work_snapshot()["counts"] == {"succeeded": 1}
    assert runner._upload_runner.run_upload_task.await_args.args[0].job_id == "pending"


@pytest.mark.asyncio
async def test_completed_upload_never_transmits_again_even_if_source_disappeared(tmp_path, monkeypatch):
    _, sessions, store = database(tmp_path / "work.db")
    add_job(sessions, youtube_video_id="already-uploaded")
    monkeypatch.setattr(upload_module, "get_session_local", lambda: sessions)
    monkeypatch.setattr(upload_module, "notify_youtube_upload_completed", AsyncMock(return_value=None))
    uploader = SimpleNamespace(upload_video=AsyncMock())
    monkeypatch.setattr(upload_module, "get_youtube_uploader", lambda: uploader)
    request = UploadRequest("pending", tmp_path / "gone.mp4", "Meeting", "unlisted")
    store.ensure("pending", "upload", upload_payload(request))
    assert await YouTubeUploadRunner(durable_store=store).run_upload_task(request)
    uploader.upload_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumable_state_survives_process_restart_after_completion_response_lost(tmp_path):
    db_path = tmp_path / "work.db"
    engine, _, store = database(db_path)
    media = tmp_path / "source.mp4"
    media.write_bytes(b"recorded-video-data")
    store.ensure("resumable", "upload", {})
    requests = []
    completed = False

    def handler(request):
        nonlocal completed
        requests.append((request.method, request.headers.get("content-range"), request.content))
        if request.method == "POST":
            return httpx.Response(
                200, headers={"Location": "https://www.googleapis.com/upload/session?upload_id=private"}
            )
        if request.headers["content-range"].startswith("bytes */"):
            assert completed
            return httpx.Response(201, json={"id": "recovered-video"})
        completed = True
        raise httpx.ReadTimeout("Connection lost after YouTube committed the video", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = YouTubeUploader(client_id="test", client_secret="test")
        first._http_client = client
        first.ensure_valid_token = AsyncMock(return_value="access-token")
        first.MAX_RETRIES = 0
        result = await first.upload_video(
            media, VideoMetadata("Meeting"), state_callback=lambda state: store.save_resume_state("resumable", state)
        )
        assert result.status == UploadStatus.FAILED
        assert store.resume_state("resumable")["session_uri"]
        engine.dispose()
        _, _, reopened = database(db_path)
        second = YouTubeUploader(client_id="test", client_secret="test")
        second._http_client = client
        second.ensure_valid_token = AsyncMock(return_value="access-token")
        result = await second.upload_video(
            media,
            VideoMetadata("Meeting"),
            resume_state=reopened.resume_state("resumable"),
            state_callback=lambda state: reopened.save_resume_state("resumable", state),
        )
        assert result.status == UploadStatus.SUCCEEDED
        assert result.video_id == "recovered-video"
        assert sum(method == "POST" for method, _, _ in requests) == 1
        assert sum(bool(body) for method, _, body in requests if method == "PUT") == 1
        assert reopened.resume_state("resumable")["video_id"] == "recovered-video"


@pytest.mark.asyncio
async def test_resumable_queries_server_offset_before_sending_remaining_bytes(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"abcdefgh")
    state = {
        "session_uri": "https://www.googleapis.com/upload/session",
        "sha256": hashlib.sha256(b"abcdefgh").hexdigest(),
        "file_size": 8,
        "uploaded_bytes": 1,
    }
    requests = []

    def handler(request):
        requests.append(request)
        if request.headers["content-range"] == "bytes */8":
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        assert request.headers["content-range"] == "bytes 4-7/8"
        assert request.content == b"efgh"
        return httpx.Response(201, json={"id": "complete"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = YouTubeUploader(client_id="test", client_secret="test")
        uploader._http_client = client
        uploader.ensure_valid_token = AsyncMock(return_value="token")
        result = await uploader.upload_video(media, VideoMetadata("Meeting"), resume_state=state)
        assert result.status == UploadStatus.SUCCEEDED
        assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_source", [False, True])
async def test_ambiguous_or_changed_session_never_starts_a_duplicate_upload(tmp_path, changed_source):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    state = {
        "session_uri": "https://www.googleapis.com/upload/session",
        "sha256": hashlib.sha256(b"video").hexdigest(),
        "file_size": 5,
        "uploaded_bytes": 0,
    }
    if changed_source:
        media.write_bytes(b"other")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = YouTubeUploader(client_id="test", client_secret="test")
        uploader._http_client = client
        uploader.ensure_valid_token = AsyncMock(return_value="token")
        result = await uploader.upload_video(media, VideoMetadata("Meeting"), resume_state=state)
        assert result.status == UploadStatus.FAILED and result.needs_review and not result.retryable
        assert all(request.method != "POST" for request in requests)
        assert not requests if changed_source else len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "running", "retry_wait", "failed", "needs_review"])
async def test_unfinished_work_protects_job_and_recording_deletion(tmp_path, state):
    from fastapi import HTTPException

    from api.routes.ui_recordings import recordings_delete, recordings_delete_all
    from services.errors import ValidationError
    from services.job_actions import JobActionService

    _, sessions, store = database(tmp_path / "work.db")
    media = tmp_path / "raw.mp4"
    media.write_bytes(b"raw")
    add_job(sessions, output_path=str(media))
    store.ensure("pending", "upload", {})
    with sessions() as session:
        work = session.query(DurableWork).one()
        work.state = state
        session.commit()
        service = JobActionService(worker=None, job_runner=None)
        with pytest.raises(ValidationError, match="unfinished"):
            service.delete_job(session, "pending")
        with pytest.raises(HTTPException) as exc:
            await recordings_delete("pending", session)
        assert exc.value.status_code == 409
        assert service.delete_terminal_jobs(session).count == 0
        await recordings_delete_all(session)
        assert session.query(JobModel).count() == 1
        assert media.read_bytes() == b"raw"


def test_operator_reconciliation_requires_explicit_no_video_confirmation(tmp_path):
    _, sessions, store = database(tmp_path / "work.db")
    add_job(sessions)
    store.ensure("pending", "upload", {})
    store.save_resume_state("pending", {"session_uri": "private"})
    store.fail("pending", "upload", error="Upload session expired", retryable=False, needs_review=True)
    assert not store.retry_failed("pending", "upload")
    assert not store.resolve_upload("pending")
    assert store.resume_state("pending")["session_uri"] == "private"
    assert store.resolve_upload("pending", confirmed_absent=True)
    assert store.resume_state("pending") == {}
    assert store.snapshot()["counts"] == {"pending": 1}


def test_operator_reconciliation_can_record_existing_video_without_reupload(tmp_path):
    _, sessions, store = database(tmp_path / "work.db")
    add_job(sessions)
    store.ensure("pending", "upload", {})
    store.fail("pending", "upload", error="Upload session expired", retryable=False, needs_review=True)
    assert store.resolve_upload("pending", video_id="existing")
    with sessions() as session:
        assert session.query(JobModel).one().youtube_video_id == "existing"
    assert store.resume_state("pending")["video_id"] == "existing"


@pytest.mark.asyncio
async def test_interrupted_postprocessing_settles_raw_and_recovers_after_database_reopen(tmp_path, monkeypatch):
    from recording.post_processing import RecordingPostProcessor

    path = tmp_path / "work.db"
    engine, sessions, store = database(path)
    media = tmp_path / "raw.mp4"
    media.write_bytes(b"original-capture")
    add_job(sessions, output_path=str(media), raw_output_path=str(media))
    now = utc_now()
    job = RecordingJob(
        job_id="pending",
        provider="jitsi",
        meeting_code="room",
        display_name="Recorder",
        duration_sec=120,
        output_dir=tmp_path,
        smart_trim_enabled=False,
    )
    result = RecordingResult(
        job_id=job.job_id,
        status=JobStatus.SUCCEEDED,
        recording_info=RecordingInfo(media, media.stat().st_size, 120, now, now + timedelta(seconds=120)),
    )
    request = RecordingPostProcessingRequest(job, result, False, "unlisted", "Meeting")
    monkeypatch.setattr(runner_module, "get_session_local", lambda: sessions)
    processor = RecordingPostProcessor(session_factory=lambda: sessions, completed_notifier=AsyncMock())
    entered = asyncio.Event()

    async def blocked(_request):
        entered.set()
        await asyncio.Event().wait()

    processor.run = blocked
    first = JobRunner(post_processor=processor)
    first._start_post_processing_task(request)
    await asyncio.wait_for(entered.wait(), 1)
    await first.shutdown(post_processing_timeout_sec=0)
    assert store.snapshot()["counts"] == {"pending": 1}
    with sessions() as session:
        assert session.query(JobModel).one().status == JobStatus.SUCCEEDED.value
    engine.dispose()
    _, sessions, store = database(path)
    monkeypatch.setattr(runner_module, "get_session_local", lambda: sessions)
    notifier = AsyncMock(return_value=None)
    recovered_processor = RecordingPostProcessor(session_factory=lambda: sessions, completed_notifier=notifier)
    second = JobRunner(post_processor=recovered_processor)
    await second.recover_durable_work()
    await asyncio.gather(*list(second._post_processing_tasks))
    await second.shutdown()
    assert store.snapshot()["counts"] == {"succeeded": 1}
    notifier.assert_awaited_once()
    with sessions() as session:
        row = session.query(JobModel).one()
        assert row.trim_status == "disabled"
        assert row.raw_output_path == str(media)
        assert row.status == JobStatus.SUCCEEDED.value
    assert media.read_bytes() == b"original-capture"


@pytest.mark.asyncio
async def test_no_video_bytes_sent_until_upload_session_is_durable(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"video")
    uploader = YouTubeUploader(client_id="test", client_secret="test")
    uploader.ensure_valid_token = AsyncMock(return_value="token")
    uploader._init_resumable_upload = AsyncMock(return_value="https://www.googleapis.com/private-session")
    uploader._upload_file_resumable = AsyncMock()

    def persistence_failed(_state):
        raise RuntimeError("Database unavailable")

    result = await uploader.upload_video(media, VideoMetadata("Meeting"), state_callback=persistence_failed)
    assert result.status == UploadStatus.FAILED
    uploader._upload_file_resumable.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", ["owned", "someone-else"])
async def test_reconciliation_verifies_video_channel(tmp_path, channel_id):
    def handler(request):
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": [{"id": "video", "snippet": {"channelId": channel_id}}]})
        return httpx.Response(200, json={"items": [{"id": "owned"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = YouTubeUploader(client_id="test", client_secret="test")
        uploader._http_client = client
        uploader.ensure_valid_token = AsyncMock(return_value="token")
        info = await uploader.get_owned_video_info("video")
        assert bool(info) == (channel_id == "owned")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "running", "retry_wait", "failed", "needs_review"])
async def test_manual_upload_cannot_race_recovered_postprocessing(tmp_path, monkeypatch, state):
    _, sessions, store = database(tmp_path / "work.db")
    add_job(sessions)
    store.ensure("pending", "post_process", {})
    with sessions() as session:
        session.query(DurableWork).one().state = state
        session.commit()
    monkeypatch.setattr(runner_module, "get_session_local", lambda: sessions)
    runner = JobRunner()
    with pytest.raises(ValueError, match="post-processing"):
        runner.enqueue_upload(UploadRequest("pending", tmp_path / "video.mp4", "Meeting", "unlisted"))
    with sessions() as session:
        assert session.query(DurableWork).filter_by(kind="upload").count() == 0
    assert not runner._upload_tasks


@pytest.mark.asyncio
async def test_temporary_oauth_refresh_failure_keeps_authorization_for_retry(tmp_path):
    from uploading.youtube import OAuthToken, TokenStorage, UploadFailedError

    storage = TokenStorage(tmp_path / "token.json")
    storage.save(OAuthToken("old", "refresh", utc_now() - timedelta(minutes=10)))
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        uploader = YouTubeUploader(client_id="test", client_secret="test", token_storage=storage)
        uploader._http_client = client
        with pytest.raises(UploadFailedError):
            await uploader.ensure_valid_token()
        assert storage.load().refresh_token == "refresh"
        assert uploader.is_authorized
        assert await uploader.ensure_valid_token() == "new"
        assert storage.load().refresh_token == "refresh"


@pytest.mark.asyncio
async def test_stage_notification_shutdown_is_tracked_and_bounded():
    from scheduling.recording_executor import RecordingExecutor

    executor = RecordingExecutor(worker_provider=lambda: None)
    entered = asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()

    executor._notify_stage_update = blocked
    executor._schedule_stage_notification("job", JobStatus.RECORDING)
    await asyncio.wait_for(entered.wait(), 1)
    assert len(executor._stage_notification_tasks) == 1
    await executor.shutdown(timeout_sec=0)
    assert not executor._stage_notification_tasks


@pytest.mark.asyncio
async def test_confirmed_video_receipt_recovers_without_local_file(tmp_path, monkeypatch):
    _, sessions, store = database(tmp_path / "work.db")
    add_job(sessions)
    request = UploadRequest("pending", tmp_path / "gone.mp4", "Meeting", "unlisted")
    store.ensure("pending", "upload", upload_payload(request))
    store.save_resume_state("pending", {"video_id": "confirmed-video"})
    notifier = AsyncMock(return_value=None)
    monkeypatch.setattr(upload_module, "notify_youtube_upload_completed", notifier)
    runner = YouTubeUploadRunner(durable_store=store, session_factory=lambda: sessions)
    assert await runner.run_upload_task(request)
    with sessions() as session:
        assert session.query(JobModel).one().youtube_video_id == "confirmed-video"
    notifier.assert_awaited_once()


def test_atomic_token_save_preserves_old_authorization_on_write_failure(tmp_path, monkeypatch):
    import uploading.youtube as youtube_module
    from uploading.youtube import OAuthToken, TokenStorage

    storage = TokenStorage(tmp_path / "token.json")
    storage.save(OAuthToken("old", "refresh", utc_now()))

    def fail_fsync(_fd):
        raise OSError("Disk write failed")

    monkeypatch.setattr(youtube_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        storage.save(OAuthToken("new", "refresh", utc_now()))
    assert storage.load().access_token == "old"
    assert not list(tmp_path.glob(".youtube-token-*"))
    assert storage.storage_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_recovered_postprocessing_preserves_operator_marked_raw_mkv(tmp_path):
    from recording.post_processing import RecordingPostProcessor

    path = tmp_path / "original.mkv"
    path.write_bytes(b"original")
    now = utc_now()
    result = RecordingResult(
        job_id="preserved",
        status=JobStatus.SUCCEEDED,
        raw_output_path=path,
        recording_info=RecordingInfo(path, 8, 120, now, now + timedelta(seconds=120)),
        runtime_summary={"local_recording_retention": {"preserve": True}},
    )
    job = RecordingJob(
        job_id="preserved",
        provider="jitsi",
        meeting_code="room",
        display_name="Recorder",
        duration_sec=120,
        output_dir=tmp_path,
    )
    canonicalizer = AsyncMock()
    processor = RecordingPostProcessor(canonicalizer=canonicalizer)
    await processor._canonicalize_successful_recording(job, result)
    canonicalizer.assert_not_awaited()
    assert path.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_durable_upload_preparation_keeps_preserved_original(tmp_path, monkeypatch):
    from services.storage_maintenance import CanonicalRecording

    _, sessions, store = database(tmp_path / "work.db")
    raw = tmp_path / "original.mkv"
    canonical = tmp_path / "original.mp4"
    raw.write_bytes(b"original")
    canonical.write_bytes(b"canonical")
    add_job(
        sessions,
        output_path=str(raw),
        raw_output_path=str(raw),
        runtime_summary_json=json.dumps({"local_recording_retention": {"preserve": True}}),
    )
    request = UploadRequest("pending", raw, "Meeting", "unlisted", raw_video_path=raw)
    store.ensure("pending", "upload", upload_payload(request))
    prepare = AsyncMock(return_value=CanonicalRecording(canonical, canonical.stat().st_size))
    monkeypatch.setattr(upload_module, "prepare_upload_recording_file", prepare)
    runner = YouTubeUploadRunner(durable_store=store, session_factory=lambda: sessions)
    runner.upload_to_youtube = AsyncMock(return_value=True)
    assert await runner.run_upload_task(request)
    assert prepare.await_args.kwargs["remove_source"] is False
    with sessions() as session:
        job = session.query(JobModel).one()
        assert job.raw_output_path == str(raw)
        assert job.output_path == str(canonical)
    assert raw.read_bytes() == b"original"
