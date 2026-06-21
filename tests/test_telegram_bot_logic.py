import ast
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import telegram_bot.conversation_create_schedule as create_schedule_module
import telegram_bot.handlers as handlers_module
from database.migrations import run_schema_migrations
from database.models import Base, JobStatus, Meeting, RecordingJob, TelegramUser
from scheduling.job_runner import QueueScheduleResult
from telegram_bot.conversation_common import _parse_duration_minutes, _validate_duration_minutes
from telegram_bot.conversation_create_schedule import CreateScheduleStates, create_schedule_start
from telegram_bot.handlers import _is_schedule_visible, list_handler, schedule_action_callback, stop_handler
from telegram_bot.notifications import _build_status_message
from utils.timezone import utc_now


def _make_schedule(
    *,
    schedule_type: str = "once",
    next_run_at=None,
    start_time=None,
    duration_sec: int = 3600,
):
    return SimpleNamespace(
        schedule_type=schedule_type,
        next_run_at=next_run_at,
        start_time=start_time,
        duration_sec=duration_sec,
    )


@pytest.fixture
def telegram_session_local(tmp_path, monkeypatch):
    db_path = tmp_path / "telegram.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(handlers_module, "get_db_session", lambda: SessionLocal())
    session = SessionLocal()
    try:
        session.add(TelegramUser(chat_id=123, username="tester", first_name="Test", approved=True))
        session.commit()
    finally:
        session.close()
    return SessionLocal


class FakeTelegramMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)


def _telegram_update(message: FakeTelegramMessage):
    return SimpleNamespace(
        message=message,
        callback_query=None,
        effective_chat=SimpleNamespace(id=123),
        effective_user=SimpleNamespace(username="tester", first_name="Test", last_name="User"),
    )


class FakeStopWorker:
    def __init__(self):
        self.canceled: list[str] = []
        self.active_jobs = [SimpleNamespace(job_id="job-old"), SimpleNamespace(job_id="job-new")]

    def is_job_active(self, job_id):
        return job_id in {"job-old", "job-new"}

    def request_cancel(self, job_id=None):
        self.canceled.append(job_id)
        return True


def _add_recording_job(session_local, job_id: str, status: str, *, started_offset_sec: int):
    session = session_local()
    try:
        session.add(
            RecordingJob(
                job_id=job_id,
                provider="jitsi",
                meeting_code=f"room-{job_id}",
                display_name="Recorder Bot",
                duration_sec=3600,
                status=status,
                started_at=utc_now() + timedelta(seconds=started_offset_sec),
            )
        )
        session.commit()
    finally:
        session.close()


def _add_meeting(session_local, *, name: str = "Team Sync"):
    session = session_local()
    try:
        session.add(
            Meeting(
                name=name,
                provider="jitsi",
                meeting_code="team-sync",
                default_display_name="Recorder Bot",
            )
        )
        session.commit()
    finally:
        session.close()


def test_parse_duration_minutes_supports_multiple_formats():
    assert _parse_duration_minutes("45") == 45
    assert _parse_duration_minutes("90m") == 90
    assert _parse_duration_minutes("1h30m") == 90
    assert _parse_duration_minutes("2:15") == 135
    assert _parse_duration_minutes("abc") is None


def test_validate_duration_minutes_respects_bounds(monkeypatch):
    monkeypatch.setattr(
        "telegram_bot.conversation_common.get_settings",
        lambda: SimpleNamespace(max_recording_sec=7200),
    )
    assert _validate_duration_minutes(0) == "時長必須大於 0 分鐘"
    assert _validate_duration_minutes(121) == "時長不能超過 120 分鐘"
    assert _validate_duration_minutes(120) is None


def test_conversations_module_is_compatibility_reexport_only():
    source = Path("telegram_bot/conversations.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    assert not any(isinstance(node, forbidden) for node in tree.body)
    assert "get_create_schedule_conversation" in source
    assert "_parse_duration_minutes" in source


def test_conversation_modules_only_share_common_helpers():
    repo_root = Path(__file__).resolve().parents[1]
    domain_modules = [
        "telegram_bot/conversation_create_schedule.py",
        "telegram_bot/conversation_edit_schedule.py",
        "telegram_bot/conversation_create_meeting.py",
    ]

    for module_path in domain_modules:
        source = (repo_root / module_path).read_text(encoding="utf-8")
        assert "telegram_bot.conversation_common" in source
        assert "telegram_bot.conversation_create_schedule" not in source
        assert "telegram_bot.conversation_edit_schedule" not in source
        assert "telegram_bot.conversation_create_meeting" not in source


def test_is_schedule_visible_for_upcoming_and_ongoing_once():
    now = utc_now()

    upcoming = _make_schedule(schedule_type="once", next_run_at=now + timedelta(minutes=30))
    assert _is_schedule_visible(upcoming) is True

    ongoing_once = _make_schedule(
        schedule_type="once",
        start_time=now - timedelta(minutes=10),
        duration_sec=3600,
    )
    assert _is_schedule_visible(ongoing_once) is True

    expired_once = _make_schedule(
        schedule_type="once",
        start_time=now - timedelta(hours=2),
        duration_sec=1800,
    )
    assert _is_schedule_visible(expired_once) is False


def test_failed_notification_includes_mapped_error_reason():
    job = SimpleNamespace(
        meeting_code="room-a",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        file_size=None,
        youtube_enabled=False,
        error_code="JOIN_FAILED",
        error_message=None,
        has_screenshot=False,
        has_html_dump=False,
    )
    message = _build_status_message(job, "failed")
    assert "原因：加入會議失敗" in message


def test_failed_notification_falls_back_to_error_message():
    job = SimpleNamespace(
        meeting_code="room-b",
        started_at=None,
        completed_at=None,
        duration_actual_sec=None,
        file_size=None,
        youtube_enabled=False,
        error_code=None,
        error_message="very long internal failure message for debug",
        has_screenshot=False,
        has_html_dump=False,
    )
    message = _build_status_message(job, "failed")
    assert "原因：" in message
    assert "very long internal failure message for debug" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("queue_result", "expected_text"),
    [
        (
            QueueScheduleResult(accepted=True, status="triggered", schedule_id=1),
            "已觸發排程",
        ),
        (
            QueueScheduleResult(accepted=True, status="queued", schedule_id=1, queue_position=2),
            "已加入佇列",
        ),
        (
            QueueScheduleResult(
                accepted=False,
                status="duplicate",
                schedule_id=1,
                reason="Schedule is already running or queued",
            ),
            "已在執行或佇列中",
        ),
    ],
)
async def test_schedule_action_trigger_messages(monkeypatch, queue_result, expected_text):
    schedule = SimpleNamespace(id=1, meeting=SimpleNamespace(name="Trigger Meeting"), enabled=True)

    class FakeDb:
        def query(self, _model):
            return self

        def filter(self, *_args):
            return self

        def first(self):
            return schedule

        def commit(self):
            return None

        def close(self):
            return None

    class FakeService:
        def trigger_schedule(self, _db, _schedule_id):
            return queue_result

    class FakeQuery:
        def __init__(self):
            self.data = "trigger:1"
            self.message = SimpleNamespace(chat=SimpleNamespace(id=123))
            self.edited_text = ""

        async def answer(self, *_args, **_kwargs):
            return None

        async def edit_message_text(self, text):
            self.edited_text = text

    query = FakeQuery()
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=123),
        effective_user=SimpleNamespace(username="tester", first_name="Test", last_name="User"),
    )

    monkeypatch.setattr(handlers_module, "get_db_session", lambda: FakeDb())
    monkeypatch.setattr(handlers_module, "get_or_create_user", lambda *_args, **_kwargs: SimpleNamespace(approved=True))
    monkeypatch.setattr(handlers_module, "get_schedule_service", lambda: FakeService())

    await schedule_action_callback(update, SimpleNamespace())

    assert expected_text in query.edited_text


@pytest.mark.asyncio
async def test_stop_handler_without_job_id_cancels_latest_active_job(monkeypatch, telegram_session_local):
    _add_recording_job(telegram_session_local, "job-old", JobStatus.RECORDING.value, started_offset_sec=0)
    _add_recording_job(telegram_session_local, "job-new", JobStatus.RECORDING.value, started_offset_sec=60)
    worker = FakeStopWorker()
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: SimpleNamespace())
    message = FakeTelegramMessage()

    await stop_handler(_telegram_update(message), SimpleNamespace(args=[]))

    assert worker.canceled == ["job-new"]
    assert "job-new" in message.replies[-1]


@pytest.mark.asyncio
async def test_create_schedule_start_warns_when_recording_capacity_is_full(monkeypatch, telegram_session_local):
    _add_meeting(telegram_session_local)
    worker = SimpleNamespace(active_jobs=[SimpleNamespace(job_id="job-active")])
    runner = SimpleNamespace(
        queue_length=2,
        retry_waiting_count=1,
        max_concurrent_recordings=2,
        available_slots=0,
        queued_items=[],
        retry_waiting_items=[],
    )
    monkeypatch.setattr(create_schedule_module, "get_db_session", lambda: telegram_session_local())
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: runner)
    message = FakeTelegramMessage()
    context = SimpleNamespace(user_data={"stale": "data"})

    state = await create_schedule_start(_telegram_update(message), context)

    assert state == CreateScheduleStates.SELECT_MEETING
    assert "錄製容量已滿" in message.replies[-1]
    assert "佇列中: 2 筆" in message.replies[-1]
    assert "等待重試: 1 筆" in message.replies[-1]
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_create_schedule_start_does_not_warn_when_active_job_has_available_slot(
    monkeypatch,
    telegram_session_local,
):
    _add_meeting(telegram_session_local)
    worker = SimpleNamespace(active_jobs=[SimpleNamespace(job_id="job-active")])
    runner = SimpleNamespace(
        queue_length=0,
        retry_waiting_count=0,
        max_concurrent_recordings=2,
        available_slots=1,
        queued_items=[],
        retry_waiting_items=[],
    )
    monkeypatch.setattr(create_schedule_module, "get_db_session", lambda: telegram_session_local())
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: runner)
    message = FakeTelegramMessage()

    state = await create_schedule_start(_telegram_update(message), SimpleNamespace(user_data={}))

    assert state == CreateScheduleStates.SELECT_MEETING
    assert "錄製容量已滿" not in message.replies[-1]
    assert "排隊等待" not in message.replies[-1]


@pytest.mark.asyncio
async def test_stop_handler_with_job_id_cancels_specified_active_job(monkeypatch, telegram_session_local):
    _add_recording_job(telegram_session_local, "job-old", JobStatus.RECORDING.value, started_offset_sec=0)
    _add_recording_job(telegram_session_local, "job-new", JobStatus.RECORDING.value, started_offset_sec=60)
    worker = FakeStopWorker()
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: SimpleNamespace())
    message = FakeTelegramMessage()

    await stop_handler(_telegram_update(message), SimpleNamespace(args=["job-old"]))

    assert worker.canceled == ["job-old"]
    assert "job-old" in message.replies[-1]


@pytest.mark.asyncio
async def test_stop_handler_without_job_id_ignores_stale_db_active_job(monkeypatch, telegram_session_local):
    _add_recording_job(telegram_session_local, "job-stale", JobStatus.RECORDING.value, started_offset_sec=120)
    _add_recording_job(telegram_session_local, "job-new", JobStatus.RECORDING.value, started_offset_sec=60)
    worker = FakeStopWorker()
    worker.active_jobs = [SimpleNamespace(job_id="job-new")]
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: SimpleNamespace())
    message = FakeTelegramMessage()

    await stop_handler(_telegram_update(message), SimpleNamespace(args=[]))

    assert worker.canceled == ["job-new"]
    assert "job-new" in message.replies[-1]
    assert "job-stale" not in message.replies[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_id", "source", "expected_error_message"),
    [
        ("job-queued", "fifo", "Canceled while queued"),
        ("job-retry", "retry_waiting", "Canceled while waiting to retry"),
    ],
)
async def test_stop_handler_with_job_id_cancels_queued_or_retry_job(
    monkeypatch,
    telegram_session_local,
    job_id,
    source,
    expected_error_message,
):
    _add_recording_job(telegram_session_local, job_id, JobStatus.QUEUED.value, started_offset_sec=0)
    worker = SimpleNamespace(active_jobs=[], is_job_active=lambda _job_id: False)

    class FakeRunner:
        def __init__(self):
            self.canceled = []

        def cancel_queued_job_for_action(self, requested_job_id: str):
            self.canceled.append(requested_job_id)
            return SimpleNamespace(removed=requested_job_id == job_id, source=source, schedule_id=None)

    runner = FakeRunner()
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: runner)
    message = FakeTelegramMessage()

    await stop_handler(_telegram_update(message), SimpleNamespace(args=[job_id]))

    assert runner.canceled == [job_id]
    assert "已取消 job" in message.replies[-1]
    assert "已發送停止指令" not in message.replies[-1]
    session = telegram_session_local()
    try:
        db_job = session.query(RecordingJob).filter(RecordingJob.job_id == job_id).one()
        assert db_job.status == JobStatus.CANCELED.value
        assert db_job.error_message == expected_error_message
    finally:
        session.close()


@pytest.mark.asyncio
async def test_list_handler_reports_queue_and_retry_waiting_counts(monkeypatch, telegram_session_local):
    worker = SimpleNamespace(is_busy=False, active_jobs=[])
    runner = SimpleNamespace(queue_length=2, retry_waiting_count=1)
    monkeypatch.setattr("recording.worker.get_worker", lambda: worker)
    monkeypatch.setattr("scheduling.job_runner.get_job_runner", lambda: runner)
    message = FakeTelegramMessage()

    await list_handler(_telegram_update(message), SimpleNamespace())

    assert "佇列中: 2 筆" in message.replies[-1]
    assert "等待重試: 1 筆" in message.replies[-1]
