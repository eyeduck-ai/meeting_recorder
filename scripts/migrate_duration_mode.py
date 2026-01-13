"""Database migration: Add duration_mode column to schedules table."""
import sqlite3
from pathlib import Path

db_path = Path("data/app.db")

if db_path.exists():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if column already exists
    cursor.execute("PRAGMA table_info(schedules)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if "duration_mode" not in columns:
        cursor.execute("ALTER TABLE schedules ADD COLUMN duration_mode VARCHAR(32) DEFAULT 'fixed'")
        conn.commit()
        print("Migration successful: added duration_mode column")
    else:
        print("Column duration_mode already exists")
    
    conn.close()
else:
    print(f"Database not found at {db_path}")
