from types import SimpleNamespace

from sqlalchemy import create_engine


def test_models_import_does_not_create_engine(monkeypatch):
    import database.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_SessionLocal", None)

    import database.models  # noqa: F401

    assert session_module._engine is None
    assert session_module._SessionLocal is None


def test_get_engine_lazily_creates_engine(tmp_path, monkeypatch):
    import database.session as session_module

    db_path = tmp_path / "lazy.db"
    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_SessionLocal", None)
    monkeypatch.setattr(session_module, "get_settings", lambda: SimpleNamespace(database_url=f"sqlite:///{db_path}"))

    assert session_module._engine is None

    engine = session_module.get_engine()
    try:
        assert engine is session_module._engine
        assert engine.dialect.name == "sqlite"
    finally:
        engine.dispose()


def test_init_db_creates_tables_and_runs_schema_migrations(monkeypatch):
    import database.session as session_module

    fake_engine = object()
    calls = []

    def fake_create_all(*, bind):
        calls.append(("create_all", bind))

    def fake_run_schema_migrations(engine):
        calls.append(("run_schema_migrations", engine))

    monkeypatch.setattr(session_module, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(session_module.Base.metadata, "create_all", fake_create_all)
    monkeypatch.setattr(session_module, "run_schema_migrations", fake_run_schema_migrations)

    session_module.init_db()

    assert calls == [
        ("create_all", fake_engine),
        ("run_schema_migrations", fake_engine),
    ]


def test_run_schema_migrations_adds_existing_compat_columns(tmp_path):
    from database.migrations import run_schema_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE recording_jobs (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE detection_logs (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE schedules (id INTEGER PRIMARY KEY)")

        run_schema_migrations(engine)

        with engine.connect() as connection:
            job_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(recording_jobs)").fetchall()}
            schedule_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(schedules)").fetchall()}
            detection_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(detection_logs)").fetchall()
            }

        assert {"attempt_no", "retry_count", "failure_stage", "last_ffmpeg_exit_code", "runtime_summary_json"} <= (
            job_columns
        )
        assert {"last_triggered_at", "last_started_at", "last_completed_at"} <= schedule_columns
        assert "attempt_no" in detection_columns
    finally:
        engine.dispose()


def test_models_run_schema_migrations_alias_remains_available():
    from database.migrations import run_schema_migrations
    from database.models import _run_schema_migrations

    assert _run_schema_migrations is run_schema_migrations
