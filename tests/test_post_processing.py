import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.migrations import run_schema_migrations
from database.models import Base, DetectionLog, JobStatus
from database.models import RecordingJob as RecordingJobModel
from database.session import JobRepository
from recording.activity import TrimDecision
from recording.ffmpeg_pipeline import RecordingInfo
from recording.job_types import RecordingJob, RecordingResult
from recording.post_processing import ActivityAnalysisLimiter, RecordingPostProcessingRequest, RecordingPostProcessor
from services.storage_maintenance import CanonicalRecording
from utils.timezone import utc_now


@pytest.fixture
def post_processing_session_local(tmp_path):
    db_path = tmp_path / "post-processing.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _create_db_job(session_local, job: RecordingJob) -> None:
    session = session_local()
    try:
        JobRepository(session).create(
            job_id=job.job_id,
            schedule_id=None,
            provider=job.provider,
            meeting_code=job.meeting_code,
            display_name=job.display_name,
            base_url=job.base_url,
            duration_sec=job.duration_sec,
            lobby_wait_sec=job.lobby_wait_sec,
            status=JobStatus.FINALIZING.value,
            attempt_no=job.attempt_no,
            retry_count=max(0, job.attempt_no - 1),
        )
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_activity_analysis_limiter_limits_parallel_completed_file_work():
    limiter = ActivityAnalysisLimiter(max_parallel=1)
    running = 0
    max_running = 0
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    order = []

    async def run_analysis(name):
        nonlocal max_running, running
        async with limiter.slot():
            running += 1
            max_running = max(max_running, running)
            order.append(name)
            if name == "first":
                entered_first.set()
                await release_first.wait()
            running -= 1

    first = asyncio.create_task(run_analysis("first"))
    await entered_first.wait()
    second = asyncio.create_task(run_analysis("second"))
    await asyncio.sleep(0)

    assert order == ["first"]
    assert max_running == 1

    release_first.set()
    await asyncio.gather(first, second)

    assert order == ["first", "second"]
    assert max_running == 1


@pytest.mark.asyncio
async def test_smart_trim_summary_records_expected_and_actual_duration(tmp_path):
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
    job = RecordingJob(
        job_id="job123",
        provider="jitsi",
        meeting_code="room",
        display_name="Bot",
        duration_sec=60,
        output_dir=tmp_path,
        diagnostics_dir=tmp_path,
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

    processor = RecordingPostProcessor(
        analyzer_factory=FakeAnalyzer,
        trim_func=fake_trim_recording,
        canonicalizer=AsyncMock(return_value=None),
    )

    await processor.apply_smart_trim(job=job, result=result, diagnostics_dir=tmp_path)

    assert result.trim_diagnostics["trim_output_expected_duration_sec"] == 8.0
    assert result.trim_diagnostics["trim_output_actual_duration_sec"] == 8.75
    assert result.recording_info.duration_sec == 8.75


@pytest.mark.asyncio
async def test_post_processing_success_canonicalizes_and_returns_upload_request(
    post_processing_session_local, tmp_path
):
    raw_path = tmp_path / "recording.mkv"
    mp4_path = tmp_path / "recording.mp4"
    raw_path.write_bytes(b"raw")
    mp4_path.write_bytes(b"mp4")
    now = utc_now()
    job = RecordingJob(
        job_id="post-canon",
        provider="jitsi",
        meeting_code="room",
        display_name="Bot",
        duration_sec=60,
        output_dir=tmp_path,
        diagnostics_dir=tmp_path,
        smart_trim_enabled=False,
    )
    _create_db_job(post_processing_session_local, job)
    result = RecordingResult(
        job_id=job.job_id,
        status=JobStatus.SUCCEEDED,
        attempt_no=1,
        recording_info=RecordingInfo(
            output_path=raw_path,
            file_size=raw_path.stat().st_size,
            duration_sec=30.0,
            start_time=now,
            end_time=now + timedelta(seconds=30),
        ),
        runtime_summary={"recording_info": {"output_path": str(raw_path), "file_size": raw_path.stat().st_size}},
        recording_started_at=now,
    )
    processor = RecordingPostProcessor(
        canonicalizer=AsyncMock(
            return_value=CanonicalRecording(output_path=mp4_path, file_size=mp4_path.stat().st_size)
        ),
        session_factory=lambda: post_processing_session_local,
        completed_notifier=AsyncMock(),
    )

    upload_request = await processor.run(
        RecordingPostProcessingRequest(
            job=job,
            result=result,
            youtube_enabled=True,
            youtube_privacy="private",
            meeting_name="Weekly Review",
        )
    )

    assert upload_request is not None
    assert upload_request.video_path == mp4_path
    assert upload_request.raw_video_path == mp4_path
    assert upload_request.cleanup_video_path_after_success is None
    assert upload_request.privacy == "private"

    session = post_processing_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
        assert db_job.status == JobStatus.SUCCEEDED.value
        assert db_job.output_path == str(mp4_path)
        assert db_job.raw_output_path == str(mp4_path)
        assert db_job.file_size == mp4_path.stat().st_size
        assert db_job.runtime_summary["recording_info"]["output_path"] == str(mp4_path)
        assert db_job.trim_status == "disabled"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_detection_log_failure_does_not_block_terminal_success(post_processing_session_local, tmp_path):
    raw_path = tmp_path / "recording.mp4"
    raw_path.write_bytes(b"raw")
    now = utc_now()
    job = RecordingJob(
        job_id="post-detection-fail",
        provider="jitsi",
        meeting_code="room",
        display_name="Bot",
        duration_sec=60,
        output_dir=tmp_path,
        diagnostics_dir=tmp_path,
        smart_trim_enabled=False,
    )
    _create_db_job(post_processing_session_local, job)
    result = RecordingResult(
        job_id=job.job_id,
        status=JobStatus.SUCCEEDED,
        attempt_no=1,
        recording_info=RecordingInfo(
            output_path=raw_path,
            file_size=raw_path.stat().st_size,
            duration_sec=30.0,
            start_time=now,
            end_time=now + timedelta(seconds=30),
        ),
        runtime_summary={"recording_info": {"output_path": str(raw_path), "file_size": raw_path.stat().st_size}},
    )

    class DetectionLogFailingSession:
        def __init__(self, session):
            self._session = session

        def add(self, obj):
            if isinstance(obj, DetectionLog):
                raise RuntimeError("detection write failed")
            return self._session.add(obj)

        def __getattr__(self, name):
            return getattr(self._session, name)

    processor = RecordingPostProcessor(
        canonicalizer=AsyncMock(return_value=None),
        session_factory=lambda: (lambda: DetectionLogFailingSession(post_processing_session_local())),
        completed_notifier=AsyncMock(),
    )

    upload_request = await processor.run(
        RecordingPostProcessingRequest(
            job=job,
            result=result,
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
        )
    )

    assert upload_request is None
    session = post_processing_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
        assert db_job.status == JobStatus.SUCCEEDED.value
        assert db_job.output_path == str(raw_path)
        assert db_job.trim_status == "disabled"
        assert db_job.error_message is None
        assert session.query(DetectionLog).count() == 0
    finally:
        session.close()


@pytest.mark.asyncio
async def test_post_processing_trimmed_recording_normalizes_upload_and_db_paths(
    post_processing_session_local, tmp_path
):
    raw_path = tmp_path / "recording.mkv"
    trimmed_mkv_path = tmp_path / "recording.trimmed.mkv"
    trimmed_mp4_path = tmp_path / "recording.trimmed.mp4"
    raw_path.write_bytes(b"raw")
    trimmed_mkv_path.write_bytes(b"trimmed")
    trimmed_mp4_path.write_bytes(b"mp4")
    now = utc_now()
    job = RecordingJob(
        job_id="post-trim-canon",
        provider="jitsi",
        meeting_code="trim-room",
        display_name="Bot",
        duration_sec=60,
        output_dir=tmp_path,
        diagnostics_dir=tmp_path,
        smart_trim_enabled=True,
    )
    _create_db_job(post_processing_session_local, job)

    class FakeAnalyzer:
        def __init__(self, _config):
            pass

        async def analyze(self, _path):
            return TrimDecision(
                status="trimmed",
                reason="media activity boundaries detected",
                trim_start_sec=10.0,
                trim_end_sec=55.0,
                duration_sec=60.0,
                diagnostics={},
            )

    async def fake_trim_recording(**_kwargs):
        return RecordingInfo(
            output_path=trimmed_mkv_path,
            file_size=trimmed_mkv_path.stat().st_size,
            duration_sec=45.0,
            start_time=now,
            end_time=now + timedelta(seconds=45),
        )

    result = RecordingResult(
        job_id=job.job_id,
        status=JobStatus.SUCCEEDED,
        attempt_no=1,
        recording_info=RecordingInfo(
            output_path=raw_path,
            file_size=raw_path.stat().st_size,
            duration_sec=60.0,
            start_time=now,
            end_time=now + timedelta(seconds=60),
        ),
        runtime_summary={"recording_info": {"output_path": str(raw_path), "file_size": raw_path.stat().st_size}},
        recording_started_at=now,
    )
    processor = RecordingPostProcessor(
        analyzer_factory=FakeAnalyzer,
        trim_func=fake_trim_recording,
        canonicalizer=AsyncMock(
            return_value=CanonicalRecording(output_path=trimmed_mp4_path, file_size=trimmed_mp4_path.stat().st_size)
        ),
        session_factory=lambda: post_processing_session_local,
        completed_notifier=AsyncMock(),
    )

    upload_request = await processor.run(
        RecordingPostProcessingRequest(
            job=job,
            result=result,
            youtube_enabled=True,
            youtube_privacy="unlisted",
            meeting_name="Trimmed Review",
        )
    )

    assert upload_request is not None
    assert upload_request.video_path == trimmed_mp4_path
    assert upload_request.raw_video_path == raw_path
    assert upload_request.cleanup_video_path_after_success == trimmed_mp4_path

    session = post_processing_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
        assert db_job.output_path == str(trimmed_mp4_path)
        assert db_job.raw_output_path == str(raw_path)
        assert db_job.trimmed_output_path == str(trimmed_mp4_path)
        assert db_job.trim_status == "trimmed"
        assert db_job.runtime_summary["trim"]["trimmed_output_path"] == str(trimmed_mp4_path)
    finally:
        session.close()


@pytest.mark.asyncio
async def test_post_processing_unexpected_trim_error_preserves_raw_recording_success(
    post_processing_session_local, tmp_path
):
    raw_path = tmp_path / "recording.mkv"
    raw_path.write_bytes(b"raw")
    now = utc_now()
    job = RecordingJob(
        job_id="post-trim-error",
        provider="jitsi",
        meeting_code="room",
        display_name="Bot",
        duration_sec=60,
        output_dir=tmp_path,
        diagnostics_dir=tmp_path,
        smart_trim_enabled=True,
    )
    _create_db_job(post_processing_session_local, job)

    class BrokenAnalyzer:
        def __init__(self, _config):
            pass

        async def analyze(self, _path):
            raise RuntimeError("probe crashed")

    result = RecordingResult(
        job_id=job.job_id,
        status=JobStatus.SUCCEEDED,
        attempt_no=1,
        recording_info=RecordingInfo(
            output_path=raw_path,
            file_size=raw_path.stat().st_size,
            duration_sec=30.0,
            start_time=now,
            end_time=now + timedelta(seconds=30),
        ),
        runtime_summary={"recording_info": {"output_path": str(raw_path), "file_size": raw_path.stat().st_size}},
    )
    processor = RecordingPostProcessor(
        analyzer_factory=BrokenAnalyzer,
        canonicalizer=AsyncMock(return_value=None),
        session_factory=lambda: post_processing_session_local,
        completed_notifier=AsyncMock(),
    )

    upload_request = await processor.run(
        RecordingPostProcessingRequest(
            job=job,
            result=result,
            youtube_enabled=False,
            youtube_privacy="unlisted",
            meeting_name=None,
        )
    )

    assert upload_request is None
    session = post_processing_session_local()
    try:
        db_job = session.query(RecordingJobModel).filter(RecordingJobModel.job_id == job.job_id).one()
        assert db_job.status == JobStatus.SUCCEEDED.value
        assert db_job.output_path == str(raw_path)
        assert db_job.trim_status == "failed"
        assert "probe crashed" in db_job.error_message
    finally:
        session.close()
