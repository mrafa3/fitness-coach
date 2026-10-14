"""Mark a workout as not yours (for example, someone else used your treadmill profile).

  .venv/bin/python exclude.py              # list the last 14 days, pick one to exclude
  .venv/bin/python exclude.py --excluded   # show what you've excluded
  .venv/bin/python exclude.py --restore    # pick an excluded workout to bring back

An excluded workout is removed from your data and remembered, so Health Auto Export
re-sending it never brings it back. Restoring forgets the exclusion; the workout returns
the next time your phone sends it (if it's still in the date range being sent).
Afterwards the report is rebuilt and that week's Week plan summary is refreshed on the
next calendar sync.
"""
import sys
from datetime import date, datetime, timedelta

from db import connect


def _describe(r) -> str:
    when = datetime.fromisoformat(r["started_at"]).strftime("%a %b %-d, %-I:%M %p")
    mins = round((r["elapsed_time_s"] or r["moving_time_s"] or 0) / 60)
    bits = [when, r["sport_type"], f"{mins} min"]
    if r["distance_m"]:
        bits.append(f"{r['distance_m'] / 1609.344:.2f} mi")
    if r["source"]:
        bits.append(f"({r['source']})")
    return "  ".join(bits)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled; nothing changed.")


def _pick(rows, prompt: str):
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}. {_describe(r)}")
    choice = _ask(f"\n{prompt} (number, or Enter to cancel): ")
    if not choice:
        return None
    if not (choice.isascii() and choice.isdigit()) or not 1 <= int(choice) <= len(rows):
        sys.exit("Not a number from the list; nothing changed.")
    return rows[int(choice) - 1]


def _refresh(conn, started_at: str) -> None:
    """Rebuild the report, and let the affected weeks' summaries be remade on the next sync."""
    d = date.fromisoformat(started_at[:10])
    monday = d - timedelta(days=d.weekday())
    conn.executemany("DELETE FROM plan_summaries WHERE week_start=?",
                     [(monday.isoformat(),), ((monday + timedelta(days=7)).isoformat(),)])
    conn.commit()
    import report
    report.build()
    print("The report is updated. The calendar's Week plan note refreshes on the next sync "
          "(Sunday, or now with: .venv/bin/python gcal.py).")


def main(argv: list[str]) -> None:
    conn = connect()
    if "--excluded" in argv or "--restore" in argv:
        rows = conn.execute("""SELECT e.id, e.started_at, e.sport_type, e.note, e.excluded_at,
                                      NULL AS elapsed_time_s, NULL AS moving_time_s, NULL AS distance_m,
                                      NULL AS source
                               FROM excluded_workouts e ORDER BY e.started_at DESC""").fetchall()
        if not rows:
            print("You haven't excluded any workouts.")
            return
        if "--excluded" in argv:
            for r in rows:
                print(f"  {datetime.fromisoformat(r['started_at']):%a %b %-d, %-I:%M %p}  {r['sport_type']}"
                      + (f"  ({r['note']})" if r["note"] else ""))
            return
        r = _pick(rows, "Restore which one")
        if r:
            conn.execute("DELETE FROM excluded_workouts WHERE id=?", (r["id"],))
            conn.commit()
            print("Restored. It comes back the next time your phone sends that day.")
        return

    since = (date.today() - timedelta(days=14)).isoformat()
    rows = conn.execute("SELECT * FROM activities WHERE started_at >= ? ORDER BY started_at DESC",
                        (since,)).fetchall()
    if not rows:
        print("No workouts in the last 14 days.")
        return
    print("Workouts from the last 14 days:\n")
    r = _pick(rows, "Exclude which one")
    if not r:
        print("Nothing changed.")
        return
    note = _ask("Optional note (e.g. 'not my run'), or Enter: ")[:80] or None
    if _ask(f"Exclude {_describe(r)}? [y/N] ").lower() != "y":
        print("Nothing changed.")
        return
    with conn:
        conn.execute("INSERT OR REPLACE INTO excluded_workouts VALUES (?,?,?,?,?)",
                     (r["id"], r["started_at"], r["sport_type"], note,
                      datetime.now().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM activities WHERE id=?", (r["id"],))
    print("Excluded.")
    _refresh(conn, r["started_at"])


if __name__ == "__main__":
    main(sys.argv[1:])
