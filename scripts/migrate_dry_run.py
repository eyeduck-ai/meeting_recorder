"""Database migration: Add dry_run column to schedules table."""
import sqlite3
from pathlib import Path

db_path = Path("data/app.db")

if db_path.exists():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(schedules)")
    columns = [row[1] for row in cursor.fetchall()]

    if "dry_run" not in columns:
        cursor.execute("ALTER TABLE schedules ADD COLUMN dry_run BOOLEAN DEFAULT 0")
        conn.commit()
        print("Migration successful: added dry_run column")
    else:
        print("Column dry_run already exists")

    conn.close()
else:
    print(f"Database not found at {db_path}")
