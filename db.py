import hashlib
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("FC_HOME", Path.home() / ".fitness-coach"))
DB_PATH = HOME / "workout.db"


def _zone():
    """The time zone in coach.toml, for start times that came without an offset.
    Raises if coach.toml can't give one: guessing would produce wrong IDs."""
    import tomllib
    from zoneinfo import ZoneInfo
    tz = tomllib.loads((Path(__file__).parent / "coach.toml").read_text())["schedule"]["timezone"]
    return ZoneInfo(tz)


def start_key(local: datetime, instant: datetime | None = None) -> str:
    """A workout's start as a UTC instant. Uses the sender's offset when there is one (Health
    Auto Export always sends one); otherwise reads the local time in coach.toml's time zone."""
    t = instant if instant is not None and instant.tzinfo else local.replace(tzinfo=_zone())
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workout_id(sport_type: str, start_utc: str, source: str | None) -> str:
    """One ID per real workout: its type, start instant (to the second) and recording app.
    Health Auto Export changes its own workout ids on every send, so those can't be used."""
    return "w-" + hashlib.sha1(f"{sport_type}|{start_utc}|{source or ''}".encode()).hexdigest()[:32]


def _backup_once(conn: sqlite3.Connection) -> None:
    """A copy of the database from before the cleanup, readable only by you. Delete it once
    you're happy with the cleanup: ~/.fitness-coach/workout.db.before-dedupe"""
    bak = HOME / "workout.db.before-dedupe"
    if bak.exists():
        return
    import tempfile
    fd, name = tempfile.mkstemp(dir=HOME, prefix=".before-dedupe-", suffix=".tmp")  # 0600, unique per process
    os.close(fd)
    tmp = Path(name)
    try:
        # copy through a second connection: this one holds the write lock, which makes sqlite's
        # backup wait on itself; readers are still allowed, and no one else can write meanwhile
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        ok = (dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and
              dst.execute("SELECT 1 FROM sqlite_master WHERE name='activities'").fetchone() is not None)
        dst.close()
        if ok:
            os.replace(tmp, bak)  # only a complete, checked copy takes the real name
    finally:
        tmp.unlink(missing_ok=True)


def _dedupe_activities(conn: sqlite3.Connection) -> None:
    """Earlier versions keyed workouts on Health Auto Export's ids, which change every send,
    so one workout could be stored several times. Keep the newest copy of each, under its
    stable ID. A backup of the database is saved first, next to it."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # one cleanup at a time, and no other writer mid-way
    try:
        if not conn.execute("SELECT 1 FROM activities WHERE id NOT LIKE 'w-%' LIMIT 1").fetchone():
            conn.rollback()  # another process finished the cleanup while this one waited
            return
        _backup_once(conn)
        # rows saved before the app name was recorded: drop them when a named copy exists
        dropped = conn.execute("""SELECT a.started_at FROM activities a WHERE a.source IS NULL AND EXISTS
                                  (SELECT 1 FROM activities b WHERE b.source IS NOT NULL
                                   AND b.sport_type = a.sport_type AND b.started_at = a.started_at)""").fetchall()
        conn.execute("""DELETE FROM activities WHERE source IS NULL AND EXISTS
                        (SELECT 1 FROM activities b WHERE b.source IS NOT NULL
                         AND b.sport_type = activities.sport_type AND b.started_at = activities.started_at)""")
        weeks = {r["started_at"][:10] for r in dropped}
        rows = conn.execute("SELECT rowid, id, sport_type, started_at, source FROM activities "
                            "ORDER BY updated_at DESC, rowid DESC").fetchall()
        seen = set()
        for r in rows:
            # rows already under a stable ID keep it (it was built from the real offset); only
            # old-style IDs are converted, using coach.toml's time zone
            nid = r["id"] if r["id"].startswith("w-") else \
                workout_id(r["sport_type"], start_key(datetime.fromisoformat(r["started_at"])), r["source"])
            if nid in seen:
                conn.execute("DELETE FROM activities WHERE rowid=?", (r["rowid"],))
                weeks.add(r["started_at"][:10])
                continue
            seen.add(nid)
            if r["id"] != nid:
                conn.execute("DELETE FROM activities WHERE id=? AND rowid<>?", (nid, r["rowid"]))
                conn.execute("UPDATE activities SET id=? WHERE rowid=?", (nid, r["rowid"]))
        # weekly summaries that counted removed copies are rebuilt on the next calendar sync:
        # the week each one belongs to, and the week after (which scored it as "last week")
        mondays = set()
        for d in weeks:
            m = date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())
            mondays |= {m.isoformat(), (m + timedelta(days=7)).isoformat()}
        conn.executemany("DELETE FROM plan_summaries WHERE week_start=?", [(m,) for m in sorted(mondays)])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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
    if conn.execute("SELECT 1 FROM activities WHERE id NOT LIKE 'w-%' LIMIT 1").fetchone():
        try:
            _zone()
        except Exception as e:  # without a time zone the IDs would be wrong; try again next time
            print(f"Skipping the duplicate-workout cleanup: coach.toml has no valid timezone ({type(e).__name__}).",
                  flush=True)
        else:
            _dedupe_activities(conn)
    return conn
