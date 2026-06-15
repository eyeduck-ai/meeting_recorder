from sqlalchemy.engine import Connection, Engine


def has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    """Check whether a SQLite table contains a column."""
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def ensure_column(connection: Connection, table_name: str, column_name: str, ddl: str) -> None:
    """Add a column if it does not already exist."""
    if not has_column(connection, table_name, column_name):
        connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")


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
        ensure_column(connection, "schedules", "last_triggered_at", "DATETIME")
        ensure_column(connection, "schedules", "last_started_at", "DATETIME")
        ensure_column(connection, "schedules", "last_completed_at", "DATETIME")
        ensure_column(connection, "detection_logs", "attempt_no", "INTEGER NOT NULL DEFAULT 1")


_has_column = has_column
_ensure_column = ensure_column
_run_schema_migrations = run_schema_migrations
