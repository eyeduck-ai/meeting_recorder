from sqlalchemy.engine import Connection, Engine

LEGACY_AUTO_DETECT_MIGRATION_KEY = "migration_legacy_auto_detect_removed"


def has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    """Check whether a SQLite table contains a column."""
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def has_table(connection: Connection, table_name: str) -> bool:
    """Check whether a SQLite database contains a table."""
    row = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_column(connection: Connection, table_name: str, column_name: str, ddl: str) -> None:
    """Add a column if it does not already exist."""
    if not has_column(connection, table_name, column_name):
        connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")


def ensure_index(connection: Connection, table_name: str, index_name: str, columns: tuple[str, ...]) -> None:
    """Create an index if its table and columns exist."""
    if not has_table(connection, table_name):
        return
    table_columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}
    if not set(columns) <= table_columns:
        return
    connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({', '.join(columns)})")


def migrate_legacy_auto_detect_schedules(connection: Connection) -> None:
    """Move existing schedules from legacy provider auto-detect to smart-boundary defaults once."""
    if not has_table(connection, "app_settings") or not has_table(connection, "schedules"):
        return

    marker = connection.exec_driver_sql(
        "SELECT value FROM app_settings WHERE key = ?",
        (LEGACY_AUTO_DETECT_MIGRATION_KEY,),
    ).fetchone()
    if marker:
        return

    required_columns = {
        "duration_mode",
        "duration_sec",
        "min_duration_sec",
        "auto_detect_mode",
        "dry_run",
        "smart_trim_enabled",
        "dynamic_extension_enabled",
        "dynamic_extension_idle_sec",
        "dynamic_extension_max_sec",
    }
    schedule_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(schedules)").fetchall()}
    if required_columns <= schedule_columns:
        connection.exec_driver_sql(
            """
            UPDATE schedules
            SET
                duration_sec = CASE
                    WHEN duration_mode = 'auto' AND min_duration_sec IS NOT NULL AND min_duration_sec > 0
                    THEN min_duration_sec
                    ELSE duration_sec
                END,
                duration_mode = 'fixed',
                auto_detect_mode = NULL,
                dry_run = 0,
                smart_trim_enabled = NULL,
                dynamic_extension_enabled = NULL,
                dynamic_extension_idle_sec = NULL,
                dynamic_extension_max_sec = NULL
            """
        )

    app_settings_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(app_settings)").fetchall()}
    if "updated_at" in app_settings_columns:
        connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (LEGACY_AUTO_DETECT_MIGRATION_KEY, "true"),
        )
    else:
        connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (LEGACY_AUTO_DETECT_MIGRATION_KEY, "true"),
        )


def run_schema_migrations(engine: Engine) -> None:
    """Apply idempotent schema migrations for existing SQLite databases."""
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        ensure_column(connection, "recording_jobs", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(connection, "recording_jobs", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "recording_jobs", "failure_stage", "VARCHAR(64)")
        ensure_column(connection, "recording_jobs", "last_ffmpeg_exit_code", "INTEGER")
        ensure_column(connection, "recording_jobs", "runtime_summary_json", "TEXT")
        ensure_column(connection, "recording_jobs", "local_recording_deleted_at", "DATETIME")
        ensure_column(connection, "recording_jobs", "local_recording_cleanup_reason", "VARCHAR(128)")
        ensure_column(connection, "recording_jobs", "raw_output_path", "VARCHAR(512)")
        ensure_column(connection, "recording_jobs", "trimmed_output_path", "VARCHAR(512)")
        ensure_column(connection, "recording_jobs", "trim_start_sec", "FLOAT")
        ensure_column(connection, "recording_jobs", "trim_end_sec", "FLOAT")
        ensure_column(connection, "recording_jobs", "trim_status", "VARCHAR(32)")
        ensure_column(connection, "recording_jobs", "trim_reason", "TEXT")
        ensure_column(connection, "recording_jobs", "dynamic_extension_stop_reason", "VARCHAR(64)")
        ensure_column(connection, "schedules", "last_triggered_at", "DATETIME")
        ensure_column(connection, "schedules", "last_started_at", "DATETIME")
        ensure_column(connection, "schedules", "last_completed_at", "DATETIME")
        ensure_column(connection, "schedules", "smart_trim_enabled", "BOOLEAN")
        ensure_column(connection, "schedules", "dynamic_extension_enabled", "BOOLEAN")
        ensure_column(connection, "schedules", "dynamic_extension_idle_sec", "INTEGER")
        ensure_column(connection, "schedules", "dynamic_extension_max_sec", "INTEGER")
        ensure_column(connection, "detection_logs", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
        ensure_index(connection, "detection_logs", "ix_detection_logs_triggered_at", ("triggered_at",))
        ensure_index(
            connection,
            "detection_logs",
            "ix_detection_logs_job_triggered_at",
            ("job_id", "triggered_at"),
        )
        ensure_index(
            connection,
            "detection_logs",
            "ix_detection_logs_type_detected_triggered_at",
            ("detector_type", "detected", "triggered_at"),
        )
        migrate_legacy_auto_detect_schedules(connection)


_has_column = has_column
_has_table = has_table
_ensure_column = ensure_column
_ensure_index = ensure_index
_migrate_legacy_auto_detect_schedules = migrate_legacy_auto_detect_schedules
_run_schema_migrations = run_schema_migrations
