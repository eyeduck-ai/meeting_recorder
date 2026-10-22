from types import SimpleNamespace

import pytest
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
        assert engine.hide_parameters is True
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


def test_sqlite_engine_uses_wal_and_busy_timeout_on_each_connection(tmp_path, monkeypatch):
    import database.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(
        session_module, "get_settings", lambda: SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'wal.db'}")
    )
    engine = session_module.get_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE sample (value INTEGER)")
            connection.exec_driver_sql("INSERT INTO sample VALUES (1)")
        with engine.connect() as writer, engine.connect() as reader:
            for connection in (writer, reader):
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
                connection.commit()
            writer.exec_driver_sql("UPDATE sample SET value=2")
            # A writer does not block an independent read of the committed view.
            assert reader.exec_driver_sql("SELECT value FROM sample").scalar() == 1
            reader.commit()
            writer.commit()
            assert reader.exec_driver_sql("SELECT value FROM sample").scalar() == 2
    finally:
        engine.dispose()


def _legacy_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'versioned.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE recording_jobs (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO recording_jobs (id) VALUES (42)")
        connection.exec_driver_sql("CREATE TABLE schedules (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE detection_logs (id INTEGER PRIMARY KEY)")
    return engine


def test_versioned_upgrade_preserves_legacy_rows_and_adds_architecture_tables(tmp_path):
    from database.migrations import run_schema_migrations

    engine = _legacy_database(tmp_path)
    try:
        run_schema_migrations(engine)
        run_schema_migrations(engine)
        with engine.connect() as connection:
            versions = connection.exec_driver_sql("SELECT version, name FROM schema_migrations ORDER BY version").all()
            tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
            assert connection.exec_driver_sql("SELECT id, attempt_no FROM recording_jobs").one() == (42, 1)
            assert versions == [(1, "compatibility_baseline"), (2, "durable_pipeline_and_notifications")]
            assert {"durable_work", "notification_deliveries"} <= tables
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
    finally:
        engine.dispose()


def test_failed_versioned_migration_rolls_back_ddl_data_and_marker(tmp_path, monkeypatch):
    import database.migrations as migrations

    engine = _legacy_database(tmp_path)
    migrations.run_schema_migrations(engine)

    def broken_upgrade(connection):
        connection.exec_driver_sql("CREATE TABLE partial_upgrade (id INTEGER)")
        connection.exec_driver_sql("UPDATE recording_jobs SET attempt_no=999")
        raise RuntimeError("injected upgrade failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (3, "broken", broken_upgrade)))
    try:
        with pytest.raises(RuntimeError, match="injected"):
            migrations.run_schema_migrations(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT MAX(version) FROM schema_migrations").scalar() == 2
            assert connection.exec_driver_sql("SELECT attempt_no FROM recording_jobs").scalar() == 1
            assert not migrations.has_table(connection, "partial_upgrade")
    finally:
        engine.dispose()


def test_recorded_version_one_upgrades_only_missing_version(tmp_path, monkeypatch):
    import database.migrations as migrations

    engine = _legacy_database(tmp_path)
    all_migrations = migrations.MIGRATIONS
    try:
        monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations[:1])
        migrations.run_schema_migrations(engine)
        with engine.connect() as connection:
            assert not migrations.has_table(connection, "durable_work")
            applied_at = connection.exec_driver_sql("SELECT applied_at FROM schema_migrations WHERE version=1").scalar()
        monkeypatch.setattr(migrations, "MIGRATIONS", all_migrations)
        migrations.run_schema_migrations(engine)
        with engine.connect() as connection:
            assert migrations.has_table(connection, "durable_work")
            assert migrations.has_table(connection, "notification_deliveries")
            assert (
                connection.exec_driver_sql("SELECT applied_at FROM schema_migrations WHERE version=1").scalar()
                == applied_at
            )
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM schema_migrations").scalar() == 2
    finally:
        engine.dispose()


def test_versioned_runner_rejects_newer_unknown_database(tmp_path):
    from database.migrations import run_schema_migrations

    engine = _legacy_database(tmp_path)
    run_schema_migrations(engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO schema_migrations (version, name) VALUES (999, 'future')")
        with pytest.raises(RuntimeError, match="newer"):
            run_schema_migrations(engine)
    finally:
        engine.dispose()
