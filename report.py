"""Render report.html: one chart per goal, with the plan line and a headline gap."""
import html
import json
import os
import sys
from pathlib import Path

from db import HOME, connect
from metrics import MIN_LEAN_READINGS, lean_series, load_goals, plan_line, run_series, treadmill_mph, weekly_activity, weekly_runs

OUT = HOME / "report.html"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fitness Coach</title>
<script>__CHARTJS__</script>
<script>__ADAPTER__</script>
<style>
  :root { --bg:#fbfaf7; --ink:#1d2a2a; --muted:#7a8585; --line:#0f5c57; --plan:#c9965a; --dot:#9fb7b4; --card:#fff; }
  @media (prefers-color-scheme: dark) { :root { --bg:#141a1a; --ink:#e6ecea; --muted:#8c9a98; --line:#5cc2b6; --plan:#d9a96c; --dot:#4d6461; --card:#1c2423; } }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.4 -apple-system, system-ui, sans-serif; }
  main { max-width:900px; margin:0 auto; padding:24px 16px; }
  h1 { font-size:20px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 24px; font-size:13px; }
  section { background:var(--card); border-radius:12px; padding:18px; margin-bottom:20px; }
  .head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
  .big { font-size:28px; font-weight:700; } .gap { font-size:14px; color:var(--muted); }
  .ahead { color:var(--line); } .behind { color:var(--plan); }
  .chart { position:relative; width:100%; height:260px; margin-top:12px; }  /* Chart.js fills this box */
  .focus { margin:10px 0 14px; } .notes { font-size:13px; color:var(--plan); }
  .sess { display:grid; grid-template-columns:22px 54px 1fr; gap:4px 8px; padding:8px 0; border-top:1px solid color-mix(in srgb, var(--muted) 25%, transparent); }
  .sess .d { color:var(--muted); font-size:13px; } .sess .det { grid-column:3; font-size:13px; color:var(--muted); }
  .chk { color:var(--line); font-weight:700; }
  .tag { display:inline-block; font-size:11px; line-height:1.6; padding:0 7px; border-radius:9px; color:var(--muted);
         border:1px solid color-mix(in srgb, var(--muted) 45%, transparent); vertical-align:1px; }
  .extra { border-top:0; padding-top:0; } .extra .chk { color:var(--plan); }
  .extra .tag { color:var(--plan); border-color:color-mix(in srgb, var(--plan) 55%, transparent); }
  .tbl { overflow-x:auto; margin-top:8px; }
  table { border-collapse:collapse; width:100%; font-size:12px; font-variant-numeric:tabular-nums; }
  th, td { text-align:right; padding:7px 3px; white-space:nowrap; border-top:1px solid color-mix(in srgb, var(--muted) 25%, transparent); }
  th { color:var(--muted); font-weight:500; font-size:10px; border-top:0; }
  th:first-child, td:first-child { text-align:left; } .of { color:var(--muted); }
</style></head><body><main>
<h1>Fitness Coach</h1><p class="sub">Updated __UPDATED__</p>
__FRESHNESS__
__SECTIONS__
</main>
<script>
const DATA = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
Chart.defaults.color = css('--muted'); Chart.defaults.font.family = 'system-ui, sans-serif';
// Each point can carry its own tooltip text (tip) and heading (when); plan points don't.
const day = s => new Date(s.length > 10 ? s : s + 'T12:00:00');
const fmtDay = s => day(s).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
const fmtTime = s => day(s).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
const pace = p => { const m = Math.floor(p), s = Math.round((p - m) * 60); return m + ':' + String(s).padStart(2, '0'); };
function chart(id, series, yTitle, axis) {
  // show your data plus a short look-ahead of the plan line (2 weeks, or a quarter of your
  // history, up to 8 weeks), so recent points aren't squeezed against the edge by a target
  // date months away; the header states the full target
  const DAY = 864e5;
  const xs = series.filter(s => s.label !== 'plan').flatMap(s => s.data.map(p => +day(String(p.x))));
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const ahead = Math.min(56 * DAY, Math.max(14 * DAY, (hi - lo) / 4));
  const xmin = xs.length ? lo - DAY : undefined;
  const xmax = xs.length ? hi + ahead : undefined;
  new Chart(document.getElementById(id), {
    type: 'line',
    data: { datasets: series },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'nearest', intersect: false },
      scales: { x: { type: 'time', min: xmin, max: xmax, time: { minUnit: 'day' }, grid: { display: false }, ticks: { maxTicksLimit: innerWidth < 500 ? 4 : 6, maxRotation: 0 } },
                y: { min: axis && axis[0] != null ? axis[0] : undefined, max: axis && axis[1] != null ? axis[1] : undefined,
                     title: { display: true, text: yTitle } } },
      plugins: { legend: { labels: { boxWidth: 12 } },
        tooltip: { callbacks: {
          title: items => !items.length ? '' : (items[0].raw.when || fmtDay(String(items[0].raw.x))),
          label: c => c.raw.tip || (c.dataset.label + ': ' + c.formattedValue) } } } }
  });
}
const pts = (arr, k) => arr.map(r => ({ x: r.day || r.week, y: r[k] }));
const runPts = arr => arr.map(r => ({ x: r.at || r.day, y: r.projected,
  when: fmtDay(r.at || r.day) + (r.at ? ', ' + fmtTime(r.at) : ''),
  tip: r.miles + ' mi at ' + pace(r.pace_min_mi) + '/mi' + (r.mph ? ' (' + r.mph + ' mph)' : '') +
       ' → 6 mi in ' + r.projected + ' min' }));
const weekPts = arr => arr.map(w => ({ x: w.week, y: w.best_projected, when: 'Week of ' + fmtDay(w.week),
  tip: 'Best projection ' + w.best_projected + ' min · ' + w.miles + ' mi total, longest ' + w.longest_mi + ' mi' }));
const plan = p => p.start ? [{ x: p.start[0], y: p.start[1] }, { x: p.end[0], y: p.end[1] }] : [];
if (DATA.muscle) chart('muscle', [
  { label: '14-day average', data: pts(DATA.muscle.series, 'avg'), borderColor: css('--line'), borderWidth: 2.5, pointRadius: 0, tension: 0 },
  { label: 'daily reading', data: pts(DATA.muscle.series, 'reading'), borderColor: css('--dot'), borderWidth: 1, pointRadius: 2, showLine: false },
  { label: 'plan', data: plan(DATA.muscle.plan), borderColor: css('--plan'), borderDash: [6, 5], pointRadius: 0 },
], 'lb', DATA.muscle.axis);
if (DATA.run) chart('run', [
  { label: 'weekly best projection', data: weekPts(DATA.run.weekly), borderColor: css('--line'), borderWidth: 2.5, pointRadius: 3, tension: 0 },
  { label: 'each run', data: runPts(DATA.run.runs), borderColor: css('--dot'), pointRadius: 2, showLine: false },
  { label: 'plan', data: plan(DATA.run.plan), borderColor: css('--plan'), borderDash: [6, 5], pointRadius: 0 },
], 'projected 6-mile time (min)', DATA.run.axis);
</script></body></html>"""


def _axis(goal: dict, values: list, p: dict) -> list:
    """Y-axis range: chart_min/chart_max from goals.toml if set; otherwise the data and the
    target with 5 units of room, on multiples of 5, so normal day-to-day noise looks small."""
    import math
    lo, hi = goal.get("chart_min"), goal.get("chart_max")
    ok = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if ok(lo) and ok(hi) and lo < hi:
        return [lo, hi]
    pts = [v for v in values if isinstance(v, (int, float))]
    target = (p.get("end") or [None, goal.get("target_value")])[1]
    if isinstance(target, (int, float)):
        pts.append(target)
    if not pts:
        return [None, None]
    return [5 * math.floor((min(pts) - 5) / 5), 5 * math.ceil((max(pts) + 5) / 5)]


def _section(sid: str, goal: dict, p: dict, extra: str = "", waiting: str = "No data yet") -> str:
    goal = {k: html.escape(str(v)) for k, v in goal.items()}
    s = p.get("status")
    if not s:
        head = f'<span class="gap">{html.escape(waiting)}</span>'
    else:
        cls = "ahead" if s["ahead"] or s["gap"] == 0 else "behind"
        where = "on plan" if s["gap"] == 0 else \
            f'{abs(s["gap"])} {goal["unit"]} {"ahead of" if s["ahead"] else "behind"} plan'
        head = (f'<span class="big">{s["latest"]} {goal["unit"]}</span>'
                f'<span class="gap"><span class="{cls}">{where}</span>'
                f' · target {p["end"][1]} by {goal["target_date"]}</span>')
    return (f'<section><div class="head"><strong>{goal["name"]}</strong>{head}</div>'
            f'{extra}<div class="chart"><canvas id="{sid}"></canvas></div></section>')


def _shown_week(conn):
    """The week the report's plan card shows: on Sunday, next week once its plan exists (after
    the coach runs); otherwise this week. Weeks before it go in the Past weeks table. Only on
    Sunday, so a manual mid-week plan run doesn't hide the current week's checkmarks."""
    from datetime import date, timedelta
    from coach import upcoming_monday
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    order = (upcoming_monday(), this_monday) if today.weekday() == 6 else (this_monday,)
    for ws in order:
        if conn.execute("SELECT 1 FROM coach_plans WHERE week_start=?", (ws.isoformat(),)).fetchone():
            return ws
    return this_monday


def _plan_section(conn) -> str:
    from datetime import date
    from coach import adherence
    ws = _shown_week(conn)
    plan = conn.execute("SELECT * FROM coach_plans WHERE week_start=?", (ws.isoformat(),)).fetchone()
    if not plan:
        return ""
    from datetime import timedelta
    score = adherence(conn, ws) or {"sessions": [], "completed": "", "extras": []}
    sessions = conn.execute("SELECT * FROM planned_sessions WHERE week_start=? ORDER BY day, kind",
                            (ws.isoformat(),)).fetchall()
    result = {(r["day"], r["kind"]): r for r in score["sessions"]}
    extras = score.get("extras", [])
    rows = []
    for i in range(7):  # every day of the week, planned or not
        d = ws + timedelta(days=i)
        todays = [x for x in sessions if x["day"] == d.isoformat()]
        if not todays:
            rows.append(f'<div class="sess"><span class="chk"></span><span class="d">{d:%a %-d}</span>'
                        f'<span><span class="tag">No workout planned</span></span></div>')
        for x in todays:
            rows.append(_session_row(x, result.get((x["day"], x["kind"]))))
        for a in (a for a in extras if a["day"] == d.isoformat()):
            rows.append(f'<div class="sess extra"><span class="chk">+</span><span class="d"></span>'
                        f'<span>{_extra_text(a)} <span class="tag">extra</span></span></div>')
    n_extra = len(extras)
    notes = f'<p class="notes">{html.escape(plan["guardrail_notes"])}</p>' if plan["guardrail_notes"] else ""
    tally = f'{score["completed"]} done' + (f' · +{n_extra} extra' if n_extra else "")
    return (f'<section><div class="head"><strong>Week of {ws:%b %-d}</strong>'
            f'<span class="gap">{tally}</span></div>'
            f'<p class="focus">{html.escape(plan["focus"] or "")}</p>{notes}{"".join(rows)}</section>')


def _extra_text(a: dict) -> str:
    """'Curling · 92 min' or 'Run · 3.1 mi · 31 min': a workout done beyond the plan."""
    bits = [html.escape(str(a["type"]))]
    if a.get("miles"):
        bits.append(f'{a["miles"]:.1f} mi')
    bits.append(f'{a["minutes"]} min')
    return " · ".join(bits)


def _session_row(x, r) -> str:
    """One planned session, with a check if it was done (and which day, if a day early or late)."""
    from datetime import date
    d = date.fromisoformat(x["day"])
    mark = "✓" if r and r["done"] else ""
    when = f' <span class="tag">done {date.fromisoformat(r["done_on"]):%a}</span>' if r and r.get("done_on") else ""
    meta = ""
    if x["kind"] == "run" and x["distance_mi"]:
        meta = f' · {x["distance_mi"]} mi'
        if x["target_pace"]:
            m, sec = divmod(round(x["target_pace"] * 60), 60)
            meta += f" @ {m}:{sec:02d}/mi · {treadmill_mph(x['target_pace'])} mph"
    elif x["minutes"]:
        meta = f' · {x["minutes"]} min'
        if x["kind"] == "strength":
            meta += " · any matching NTC workout"
    return (f'<div class="sess"><span class="chk">{mark}</span><span class="d">{d:%a %-d}</span>'
            f'<span>{html.escape(x["title"] or "")}{meta}{when}</span>'
            f'<span class="det">{html.escape(x["details"] or "")}</span></div>')


def _past_weeks(conn, limit: int = 12) -> str:
    """One row per planned week before the one on the plan card, newest first: sessions done, run miles against the
    plan, longest run, strength and recovery sessions done, and workouts done beyond the plan."""
    from datetime import date, timedelta
    from coach import activities_between, adherence
    shown = _shown_week(conn)
    weeks = conn.execute("SELECT week_start FROM coach_plans WHERE week_start < ? ORDER BY week_start DESC LIMIT ?",
                         (shown.isoformat(), limit)).fetchall()
    if not weeks:
        return ""
    rows = []
    for w in weeks:
        ws = date.fromisoformat(w["week_start"])
        score = adherence(conn, ws) or {"sessions": [], "completed": "0/0", "extras": []}
        runs = [a for a in activities_between(conn, ws, ws + timedelta(days=6)) if "miles" in a]
        planned_mi = conn.execute("SELECT COALESCE(SUM(distance_mi), 0) FROM planned_sessions "
                                  "WHERE week_start=? AND kind='run'", (ws.isoformat(),)).fetchone()[0]
        def kind(k):
            s = [r for r in score["sessions"] if r["kind"] == k]
            return f'{sum(r["done"] for r in s)}/{len(s)}' if s else "–"
        miles = sum(a["miles"] for a in runs)
        longest = max((a["miles"] for a in runs), default=0)
        rows.append(f'<tr><td>{ws:%b %-d}</td><td>{score["completed"]}</td>'
                    f'<td>{miles:.1f}<span class="of">/{planned_mi:.1f}</span></td>'
                    f'<td>{f"{longest:.1f}" if longest else "–"}</td><td>{kind("strength")}</td>'
                    f'<td>{kind("recovery")}</td><td>{len(score.get("extras", [])) or "–"}</td></tr>')
    return ('<section><div class="head"><strong>Past weeks</strong></div>'
            '<div class="tbl"><table><thead><tr><th>Week</th><th>Done</th><th>Miles<span class="of">/plan</span></th>'
            '<th>Longest</th><th>Strength</th><th>Recovery</th><th>Extra</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div></section>')


def _freshness(conn) -> str:
    from datetime import datetime
    row = conn.execute("SELECT last_synced_at FROM sync_state WHERE source='health'").fetchone()
    if not row:
        return '<p class="notes">No data from your iPhone yet. Check the Health Auto Export automation.</p>'
    when = datetime.fromisoformat(row["last_synced_at"])
    note = f"Last data from iPhone: {when:%a %b %-d, %-I:%M %p}"
    if (datetime.now() - when).days >= 3:
        note += " (over 3 days ago; open Health Auto Export on your phone while on home Wi-Fi)"
        return f'<p class="notes">{note}</p>'
    return f'<p class="sub">{note}</p>'


def build(quiet: bool = False) -> Path:
    conn = connect()
    goals = {g["metric"]: g for g in load_goals()}
    data, sections = {}, [_plan_section(conn), _past_weeks(conn)]

    if g := goals.get("lean_mass_lb"):
        ls = lean_series(conn)
        # wait for enough weigh-ins (MIN_LEAN_READINGS in 14 days) that one odd reading can't set the start
        p = plan_line(g, [(r["day"], r["avg"]) for r in ls if r["n"] >= MIN_LEAN_READINGS])
        data["muscle"] = {"series": ls, "plan": p,
                          "axis": _axis(g, [r["reading"] for r in ls] + [r["avg"] for r in ls], p)}
        extra = ""
        if ls:
            last = ls[-1]
            if last["weight"]:
                extra = f'<p class="gap">Weight {last["weight"]} lb · lean mass {last["lean_pct"]}% of body weight</p>'
        waiting = (f"Plan line starts after {MIN_LEAN_READINGS} readings in 14 days ({ls[-1]['n']} so far)" if ls
                   else "No readings yet")
        sections.append(_section("muscle", g, p, extra, waiting))

    if g := goals.get("projected_6mi_min"):
        runs = run_series(conn)
        weekly = weekly_runs(runs)
        p = plan_line(g, [(w["week"], w["best_projected"]) for w in weekly])
        data["run"] = {"runs": runs, "weekly": weekly, "plan": p,
                       "axis": _axis(g, [r["projected"] for r in runs], p)}
        extra = ""
        if weekly:
            w = weekly[-1]
            last = runs[-1]
            m, sec = divmod(round(last["pace_min_mi"] * 60), 60)
            speed = f' · {last["mph"]} mph' if last.get("mph") else ""
            extra = (f'<p class="gap">This week: {w["miles"]} mi total, longest run {w["longest_mi"]} mi<br>'
                     f'Last run: {last["miles"]} mi at {m}:{sec:02d}/mi{speed}</p>')
        sections.append(_section("run", g, p, extra, "No runs of a mile or more yet"))

    data["activity"] = weekly_activity(conn)
    from datetime import datetime
    vendor = Path(__file__).parent / "vendor"
    values = {
        "CHARTJS": (vendor / "chart.umd.js").read_text(),
        "ADAPTER": (vendor / "chartjs-adapter-date-fns.bundle.min.js").read_text(),
        "UPDATED": datetime.now().strftime("%b %-d, %Y %-I:%M %p"),
        "FRESHNESS": _freshness(conn),
        "SECTIONS": "\n".join(sections),
        "DATA": json.dumps(data).replace("</", "<\\/"),  # can't close the <script> early
    }
    # one pass, so text inserted from your data or the coach is never re-scanned for placeholders
    import re
    html = re.sub(r"__(CHARTJS|ADAPTER|UPDATED|FRESHNESS|SECTIONS|DATA)__", lambda m: values[m.group(1)], PAGE)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".report-", suffix=".tmp")  # unique, created 0600
    with os.fdopen(fd, "w") as f:
        f.write(html)
    os.replace(tmp, OUT)  # atomic, so an open report never shows half a file
    if not quiet:
        print(f"Report: {OUT}")
    return OUT


if __name__ == "__main__":
    build(quiet="--quiet" in sys.argv)
