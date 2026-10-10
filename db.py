import os
import sqlite3
from pathlib import Path

HOME = Path(os.environ.get("FC_HOME", Path.home() / ".fitness-coach"))
DB_PATH = HOME / "workout.db"


def connect() -> sqlite3.Connection:
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    HOME.chmod(0o700)  # health data and the receiver token live here
    conn = sqlite3.connect(DB_PATH, timeout=30)
    DB_PATH.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).parent / "schema.sql").read_text())
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
    for col, typ in (("elapsed_time_s", "INTEGER"), ("source", "TEXT"), ("avg_speed_mph", "REAL")):  # databases from earlier versions
        if col not in cols:
            conn.execute(f"ALTER TABLE activities ADD COLUMN {col} {typ}")
    return conn
