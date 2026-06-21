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
        assert {"duration_mode", "dry_run", "last_triggered_at", "last_started_at", "last_completed_at"} <= (
            schedule_columns
        )
        assert "attempt_no" in detection_columns
    finally:
        engine.dispose()


def test_run_schema_migrations_adds_detection_log_indexes_idempotently(tmp_path):
    from database.migrations import run_schema_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'detection-indexes.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE recording_jobs (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE schedules (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                """
                CREATE TABLE detection_logs (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    detector_type VARCHAR(32) NOT NULL,
                    detected BOOLEAN,
                    triggered_at DATETIME
                )
                """
            )

        run_schema_migrations(engine)
        run_schema_migrations(engine)

        with engine.connect() as connection:
            indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(detection_logs)").fetchall()}

        assert {
            "ix_detection_logs_triggered_at",
            "ix_detection_logs_job_triggered_at",
            "ix_detection_logs_type_detected_triggered_at",
        } <= indexes
    finally:
        engine.dispose()


def test_detection_log_indexes_declared_on_metadata():
    from database.models import DetectionLog

    indexes = {index.name for index in DetectionLog.__table__.indexes}

    assert {
        "ix_detection_logs_triggered_at",
        "ix_detection_logs_job_triggered_at",
        "ix_detection_logs_type_detected_triggered_at",
    } <= indexes


def test_run_schema_migrations_migrates_legacy_auto_detect_schedules_once(tmp_path):
    from database.migrations import LEGACY_AUTO_DETECT_MIGRATION_KEY, run_schema_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-auto-migration.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE schedules (
                    id INTEGER PRIMARY KEY,
                    duration_mode VARCHAR(32),
                    duration_sec INTEGER,
                    min_duration_sec INTEGER,
                    auto_detect_mode VARCHAR(16),
                    dry_run BOOLEAN,
                    smart_trim_enabled BOOLEAN,
                    dynamic_extension_enabled BOOLEAN,
                    dynamic_extension_idle_sec INTEGER,
                    dynamic_extension_max_sec INTEGER
                )
                """
            )
            connection.exec_driver_sql(
                "CREATE TABLE app_settings (key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME)"
            )
            connection.exec_driver_sql("CREATE TABLE recording_jobs (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE detection_logs (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                """
                INSERT INTO schedules (
                    id,
                    duration_mode,
                    duration_sec,
                    min_duration_sec,
                    auto_detect_mode,
                    dry_run,
                    smart_trim_enabled,
                    dynamic_extension_enabled,
                    dynamic_extension_idle_sec,
                    dynamic_extension_max_sec
                )
                VALUES
                    (1, 'auto', 14400, 1800, 'after_min', 1, 0, 0, 600, 1200),
                    (2, 'auto', 7200, 0, 'immediate', 1, 1, 0, 300, 3600),
                    (3, 'auto', 5400, NULL, 'immediate', 1, 1, 0, 300, 3600),
                    (4, 'fixed', 3600, NULL, NULL, 0, 0, 0, 300, 3600)
                """
            )

        run_schema_migrations(engine)
        run_schema_migrations(engine)

        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                """
                SELECT
                    id,
                    duration_mode,
                    duration_sec,
                    auto_detect_mode,
                    dry_run,
                    smart_trim_enabled,
                    dynamic_extension_enabled,
                    dynamic_extension_idle_sec,
                    dynamic_extension_max_sec
                FROM schedules
                ORDER BY id
                """
            ).fetchall()
            marker = connection.exec_driver_sql(
                "SELECT value FROM app_settings WHERE key = ?",
                (LEGACY_AUTO_DETECT_MIGRATION_KEY,),
            ).fetchone()

        assert rows[0] == (1, "fixed", 1800, None, 0, None, None, None, None)
        assert rows[1] == (2, "fixed", 7200, None, 0, None, None, None, None)
        assert rows[2] == (3, "fixed", 5400, None, 0, None, None, None, None)
        assert rows[3] == (4, "fixed", 3600, None, 0, None, None, None, None)
        assert marker == ("true",)
    finally:
        engine.dispose()


def test_models_do_not_reexport_database_lifecycle_helpers():
    import database.models as models

    for helper_name in (
        "get_engine",
        "get_session_local",
        "get_db",
        "init_db",
        "_run_schema_migrations",
    ):
        assert not hasattr(models, helper_name)


def test_migrations_do_not_keep_private_compat_aliases():
    import database.migrations as migrations

    for helper_name in (
        "_has_column",
        "_has_table",
        "_ensure_column",
        "_ensure_index",
        "_migrate_legacy_auto_detect_schedules",
        "_run_schema_migrations",
    ):
        assert not hasattr(migrations, helper_name)


def test_session_module_does_not_keep_unused_context_manager():
    import database.session as session_module

    assert not hasattr(session_module, "get_db_session")
