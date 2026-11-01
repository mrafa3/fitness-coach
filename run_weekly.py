"""The Sunday job: plan next week, write the calendar file, rebuild the report, and (if
you've signed in to Google) update your Training calendar, including the "Week plan"
summary event. Data arrives continuously through receiver.py, so there's nothing to sync here.

  .venv/bin/python run_weekly.py           # plan next week, unless it was already planned this weekend
  .venv/bin/python run_weekly.py --force   # make a new plan for next week anyway

So you can plan early (Saturday, or Sunday afternoon) and the 8pm run keeps that plan,
instead of replacing it with a different one. The report and calendar still refresh."""
import sys
from datetime import date, datetime, timedelta

import coach
import report


def planned_this_weekend(conn, week_start: date) -> str | None:
    """When next week's plan was made, if that was on the weekend just before it."""
    row = conn.execute("SELECT created_at FROM coach_plans WHERE week_start=?", (week_start.isoformat(),)).fetchone()
    if not row or not row["created_at"]:
        return None
    try:
        made = datetime.fromisoformat(row["created_at"])
    except ValueError:
        return None
    return f"{made:%a %-I:%M %p}" if made.date() >= week_start - timedelta(days=2) else None


def plan_is_stale(conn, week_start: date) -> bool:
    """Would the coach's rules give a different long run, or a different tempo-run unlock, now than
    when the kept plan was made? (An early plan made before the week's last run synced.)"""
    import json
    row = conn.execute("SELECT raw_json FROM coach_plans WHERE week_start=?", (week_start.isoformat(),)).fetchone()
    try:
        saved = json.loads(row["raw_json"]) if row and row["raw_json"] else {}
    except ValueError:
        return False
    if not isinstance(saved, dict):
        return False
    profile = coach.load_profile()
    now = coach.progression(conn, profile, week_start, coach.limits(conn, profile, week_start))
    ladder, unlocked = saved.get("ladder_long_mi"), saved.get("quality_unlocked")
    return ((isinstance(ladder, (int, float)) and not isinstance(ladder, bool) and abs(now["long_run_mi"] - ladder) > 0.05)
            or (isinstance(unlocked, bool) and unlocked != now["quality_unlocked"]))


def runs_still_due(conn, week_start: date) -> bool:
    """Is a run from the week being scored (the one before week_start) still due or not yet synced?"""
    adh = coach.adherence(conn, week_start - timedelta(days=7))
    return bool(adh) and coach._runs_complete(adh) is None and any(s["kind"] == "run" for s in adh["sessions"])


if __name__ == "__main__":
    from db import connect
    conn = connect()
    week_start = coach.upcoming_monday()
    made = None if "--force" in sys.argv[1:] else planned_this_weekend(conn, week_start)
    if made:
        print(f"Keeping the plan for the week of {week_start:%b %-d} made {made}. "
              "To make a new one: .venv/bin/python run_weekly.py --force", flush=True)
        try:
            stale = plan_is_stale(conn, week_start)
        except Exception as e:  # advisory only: never stop the calendar, report and Google steps
            stale = False
            print(f"Couldn't check whether the early plan is out of date: {type(e).__name__}: {e}", flush=True)
            coach.notify("Keeping your plan, but couldn't check whether it's out of date. See weekly.log")
        if stale:
            msg = ("Your early plan was made before this week's runs were all in. "
                   "To update it: .venv/bin/python run_weekly.py --force")
            print(msg, flush=True)
            coach.notify(msg)
        import calendar_ics
        try:
            calendar_ics.build()
        except Exception as e:
            print(f"Calendar file failed: {type(e).__name__}: {e}", flush=True)
            coach.notify("Keeping your plan, but writing the calendar file failed. See weekly.log")
    else:
        if runs_still_due(conn, week_start):
            print("Note: a run from this week isn't in yet, so the coach treats this week as incomplete "
                  "(the long run holds and it doesn't count toward tempo runs). If you run it today, "
                  "run this again with --force once it syncs.", flush=True)
        try:
            coach.main([])      # also writes training.ics
        except Exception as e:
            coach.notify(f"This week's plan failed: {type(e).__name__}. See ~/.fitness-coach/weekly.log")
            raise
    try:
        report.build()
    except Exception as e:  # the plan and calendar are already saved; keep going to Google
        print(f"Report failed: {type(e).__name__}: {e}", flush=True)
        coach.notify("The plan is ready, but the report failed to build. See ~/.fitness-coach/weekly.log")

    import google_api
    if google_api.configured():
        import gcal
        try:
            gcal.sync()
        except Exception as e:
            print(f"Google Calendar failed: {type(e).__name__}: {e}", flush=True)
            coach.notify("The plan is ready, but updating Google Calendar failed. See weekly.log")
            raise SystemExit(1)
