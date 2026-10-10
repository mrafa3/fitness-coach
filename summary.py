"""The weekly summary: the week's focus, each session, goal status and last week's score,
as plain text. gcal.py puts it in an all-day "Week plan" event on the plan's Monday."""
from datetime import date, timedelta

import coach
from calendar_ics import session_event


def frozen_summary(conn, ws: date) -> dict | None:
    """The summary as first made for this week's plan. It's saved the first time, so later
    data doesn't rewrite it (and a Week plan event you deleted stays deleted) until the coach
    makes a new plan for that week."""
    row = conn.execute("SELECT title, description FROM plan_summaries WHERE week_start=?",
                       (ws.isoformat(),)).fetchone()
    if row:
        return {"title": row["title"], "description": row["description"]}
    s = week_summary(conn, ws)
    if s:
        conn.execute("INSERT OR REPLACE INTO plan_summaries VALUES (?,?,?)", (ws.isoformat(), s["title"], s["description"]))
        conn.commit()
    return s


def week_summary(conn, ws: date) -> dict | None:
    """{"title", "description"} for the plan starting Monday ws, or None if there's no plan."""
    plan = conn.execute("SELECT * FROM coach_plans WHERE week_start=?", (ws.isoformat(),)).fetchone()
    if plan is None:
        return None
    rows = conn.execute("SELECT * FROM planned_sessions WHERE week_start=? ORDER BY day",
                        (ws.isoformat(),)).fetchall()
    sched = coach.load_profile()["schedule"]
    ctx = coach.build_context(conn, ws)
    last = coach.adherence(conn, ws - timedelta(days=7))

    out = [plan["focus"] or "", ""]
    for x in rows:
        e = session_event(x, sched)
        out.append(f"{e['start']:%a %-I:%M %p}  {e['title']}")
    out.append("")
    for gl in ctx["goals"]:
        st = gl.get("status")
        if st:
            where = ("on plan" if st["gap"] == 0 else
                     f"{abs(st['gap'])} {gl['unit']} {'ahead of' if st['ahead'] else 'behind'} plan")
            out.append(f"{gl['goal']}: {st['latest']} {gl['unit']} ({where}; "
                       f"target {gl['target']} by {gl['target_date']})")
        else:
            out.append(f"{gl['goal']}: not enough data yet")
    if last:
        out.append(f"Last week: {last['completed']} sessions done.")
    if plan["guardrail_notes"]:
        out += ["", "Adjusted by the coach's limits: " + plan["guardrail_notes"]]
    out += [s for s in ctx["signals"] if "Apple Health" in s]
    return {"title": "Week plan", "description": "\n".join(out).strip()}


if __name__ == "__main__":
    from db import connect
    conn = connect()
    row = conn.execute("SELECT week_start FROM coach_plans ORDER BY week_start DESC LIMIT 1").fetchone()
    s = week_summary(conn, date.fromisoformat(row["week_start"])) if row else None
    print(s["description"] if s else "No plan yet.")
