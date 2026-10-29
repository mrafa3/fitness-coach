"""What's riding on this week: the runs so far, and what finishing them (or not) means for next
week's plan. Uses the coach's own ladder and unlock rules (coach.py), so the report can't
disagree with what the coach is told on Sunday. Next week's weekly mileage cap can still
trim distances; the card describes the rules, not the coach's final plan. One known gap: if
Sunday's run hasn't synced by the time the coach runs at 8pm, the coach holds for a week
while the card (once the run arrives) shows the week as complete."""
from datetime import date, timedelta

import coach

UNLOCK_WEEKS = 3   # full run weeks in a row before quality runs are allowed (coach.progression)


def _status(session: dict | None, day: date, today: date) -> str:
    """done, short (done but under 90% of the distance), missed, or pending. A session can be
    credited a day late, so it isn't missed until the day after it has passed."""
    if session and session["done"]:
        return "short" if session.get("distance_hit") is False else "done"
    return "missed" if day + timedelta(days=1) < today else "pending"


def streak(conn, through: date, today: date) -> tuple[int, int]:
    """(current, best): planned sessions done in a row, oldest plan first. Done a day early or
    late counts; cut short counts (showing up is the point); sessions still due are skipped."""
    cur = best = 0
    weeks = conn.execute("SELECT week_start FROM coach_plans WHERE week_start <= ? ORDER BY week_start",
                         (through.isoformat(),)).fetchall()
    for w in weeks:
        adh = coach.adherence(conn, date.fromisoformat(w["week_start"]))
        for s in (adh or {}).get("sessions", []):
            st = _status(s, date.fromisoformat(s["day"]), today)
            if st in ("done", "short"):
                cur += 1
                best = max(best, cur)
            elif st == "missed":
                cur = 0
    return cur, best


def _long_cap(conn, profile: dict, week_start: date, assumed: dict) -> float:
    """coach.limits()'s long-run limit for week_start, counting the weeks in `assumed`
    ({week_start iso: longest mi}) as if they'd been run as planned."""
    g = profile["guardrails"]
    longest = {w["week"]: w["longest_mi"] for w in coach.weekly_runs(coach.run_series(conn))}
    for wk, mi in assumed.items():
        longest[wk] = max(longest.get(wk, 0), mi)
    recent = [longest[wk] for wk in sorted(longest) if wk < week_start.isoformat()][-3:]
    base = g["starting_long_run_mi"] if len(recent) < 3 else max(recent)
    return base + g["max_long_run_increase_mi"]


def _mi(x) -> str:
    """2.25 -> '2.25', 3 -> '3.0', 1.9 -> '1.9'."""
    return "–" if x is None else (f"{x:.1f}" if round(x, 1) == round(x, 2) else f"{x:.2f}")


def week_outlook(conn, ws: date, today: date | None = None) -> dict | None:
    today = today or date.today()
    adh = coach.adherence(conn, ws)
    if adh is None:
        return None
    planned = {r["day"]: r for r in conn.execute(
        "SELECT * FROM planned_sessions WHERE week_start=? AND kind='run' ORDER BY day", (ws.isoformat(),))}
    if not planned:
        return None
    scored = {s["day"]: s for s in adh["sessions"] if s["kind"] == "run"}
    runs = []
    for day, p in planned.items():
        s = scored.get(day)
        runs.append({"day": day, "mi": _mi(p["distance_mi"]), "long": p["run_type"] == "long",
                     "actual_mi": _mi(s.get("actual_mi")) if s else "–",
                     "status": _status(s, date.fromisoformat(day), today)})
    failed = any(r["status"] in ("missed", "short") for r in runs)
    all_done = all(r["status"] == "done" for r in runs)
    n = len(runs)

    profile = coach.load_profile()
    settings = coach.running_settings(profile)
    step, goal = settings["long_run_step_mi"], settings["long_run_goal_mi"]
    before = coach.planned_weeks(conn, ws)
    c = coach.consistent_weeks(before)

    # quality runs
    if failed:
        back = ws + timedelta(days=7 * (UNLOCK_WEEKS + 1))
        unlock = (f"Tempo runs pause; back {back:%b %-d} at the earliest" if c >= UNLOCK_WEEKS
                  else f"Tempo runs now unlock {back:%b %-d} at the earliest")
    else:
        k = UNLOCK_WEEKS - c - 1          # full weeks still needed after this one
        alln = "your run" if n == 1 else f"all {n}"
        when = ws + timedelta(days=7 * (UNLOCK_WEEKS - c))
        more = f"{k} more full week{'s' if k > 1 else ''}"
        if c >= UNLOCK_WEEKS:
            unlock = (f"{alln.capitalize()} done: tempo runs stay on next week" if all_done
                      else f"Finish {alln} to keep tempo runs next week")
        elif k == 0:
            unlock = (f"{alln.capitalize()} done: tempo runs unlock next week" if all_done
                      else f"Finish {alln} and tempo runs unlock next week")
        else:
            unlock = (f"{alln.capitalize()} done: {more} and tempo runs unlock {when:%b %-d}" if all_done
                      else f"Finish {alln}, then {more}: tempo runs unlock {when:%b %-d}")

    # long run: the ladder, as if this week ends the way it's going
    this = {"week_start": ws.isoformat(), "runs_complete": not failed, "planned_long_mi": coach._planned_long(conn, ws)}
    long_run = None
    L = this["planned_long_mi"]
    if L is not None:
        nxt_week = ws + timedelta(days=7)
        cap = _long_cap(conn, profile, nxt_week, {} if failed else {ws.isoformat(): L})
        nxt = round(max(min(coach.ladder_step([this] + before, step)[0], goal, cap), 1.0), 1)
        if failed:
            long_run = (f"Long run steps back to {_mi(nxt)} mi next week" if nxt < L
                        else f"Long run holds at {_mi(L)} mi next week")
        elif nxt < L - 0.05:   # held back by the long-run limit (your longest recent runs + 1 mi)
            long_run = f"Long run {_mi(L)} mi, {_mi(nxt)} next week (limited by your recent runs)"
        elif L >= goal:
            long_run = f"Long run at your {goal:g} mi goal"
        elif nxt > L:
            long_run = f"Long run {_mi(L)} mi, {_mi(nxt)} next week"
        else:
            after = {"week_start": (ws + timedelta(days=7)).isoformat(), "runs_complete": True, "planned_long_mi": nxt}
            cap2 = _long_cap(conn, profile, ws + timedelta(days=14), {ws.isoformat(): L, nxt_week.isoformat(): nxt})
            n2 = round(min(coach.ladder_step([after, this] + before, step)[0], goal, cap2), 1)
            long_run = (f"Long run {_mi(L)} mi, then {_mi(n2)} from {ws + timedelta(days=14):%b %-d}" if n2 > L
                        else f"Long run {_mi(L)} mi")
    cur, best = streak(conn, ws, today)
    return {"runs": runs, "done": sum(r["status"] in ("done", "short") for r in runs), "total": n,
            "failed": failed, "unlock": unlock, "long_run": long_run, "streak": cur, "best": best}
