"""Derived series: the plan lines and the actuals they're compared against."""
import re
import tomllib
from datetime import date, timedelta
from pathlib import Path

METERS_PER_MILE = 1609.344
MIN_RUN_MILES = 1.0      # shorter runs make the projection too optimistic
RIEGEL_EXP = 1.06
MIN_WORKOUT_SECONDS = 300  # shorter sessions (test taps, warm-ups) don't count as training

# Apple Health workout types, matched loosely so small naming differences don't matter.
RUN_WORDS = ("running", "run")
STRENGTH_WORDS = ("strength", "highintensityinterval", "hiit", "core", "functional", "kickboxing",
                  "barre", "crossfit", "boxing", "weight")
RECOVERY_WORDS = ("yoga", "flexibility", "cooldown", "pilates", "mindandbody", "walk", "stretch", "taichi",
                  "recovery")
# Everything else (cycling, elliptical, hiking, mixed cardio, cross training, ...) is "other":
# it shows up in history but doesn't count as a strength or recovery session.


def classify(workout_type: str) -> str:
    """run | strength | recovery | other, from an Apple Health workout type name."""
    n = re.sub(r"[^a-z]", "", str(workout_type).lower())
    if any(n == w or n.endswith(w) for w in RUN_WORDS) or "running" in n:
        return "run"
    if any(w in n for w in STRENGTH_WORDS):
        return "strength"
    if any(w in n for w in RECOVERY_WORDS):
        return "recovery"
    return "other"


def load_goals() -> list[dict]:
    return tomllib.loads((Path(__file__).parent / "goals.toml").read_text())["goal"]


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


# ---- body composition ------------------------------------------------------

def lean_series(conn, window: int = 14) -> list[dict]:
    """One point per day with a lean-mass reading, plus its trailing N-day average."""
    rows = conn.execute(
        """SELECT day, lean_mass_lb, weight_lb FROM weigh_ins
           WHERE lean_mass_lb IS NOT NULL ORDER BY day"""
    ).fetchall()
    out = []
    for i, r in enumerate(rows):
        d = _d(r["day"])
        vals = [x["lean_mass_lb"] for x in rows[: i + 1] if (d - _d(x["day"])).days < window]
        out.append({
            "day": r["day"],
            "reading": r["lean_mass_lb"],
            "avg": round(sum(vals) / len(vals), 2),
            "n": len(vals),
            "weight": r["weight_lb"],
            "lean_pct": round(100 * r["lean_mass_lb"] / r["weight_lb"], 1) if r["weight_lb"] else None,
        })
    return out


# ---- running ---------------------------------------------------------------

def treadmill_mph(pace_min_mi: float) -> float:
    """Treadmill speed setting (mph, 0.1 steps) for a pace in min/mile."""
    return round(60 / pace_min_mi, 1)


def projected_6mi_min(distance_m: float, moving_time_s: float) -> float:
    miles = distance_m / METERS_PER_MILE
    return moving_time_s / 60 * (6 / miles) ** RIEGEL_EXP


def run_series(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT started_at, distance_m, moving_time_s, trainer, avg_speed_mph FROM activities
           WHERE kind = 'run' AND distance_m >= ? AND moving_time_s > 0
           ORDER BY started_at""",
        (MIN_RUN_MILES * METERS_PER_MILE,),
    ).fetchall()
    return [{
        "day": r["started_at"][:10],
        "at": r["started_at"],                 # local start time, for the chart
        "miles": round(r["distance_m"] / METERS_PER_MILE, 2),
        "pace_min_mi": round(r["moving_time_s"] / 60 / (r["distance_m"] / METERS_PER_MILE), 2),
        "mph": r["avg_speed_mph"],
        "projected": round(projected_6mi_min(r["distance_m"], r["moving_time_s"]), 1),
    } for r in rows]


def weekly_runs(runs: list[dict]) -> list[dict]:
    """Best projection and longest run per week (weeks start Monday)."""
    weeks: dict[str, dict] = {}
    for r in runs:
        d = _d(r["day"])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk, {"week": wk, "best_projected": 1e9, "longest_mi": 0, "miles": 0})
        w["best_projected"] = min(w["best_projected"], r["projected"])
        w["longest_mi"] = max(w["longest_mi"], r["miles"])
        w["miles"] = round(w["miles"] + r["miles"], 2)
    return list(weeks.values())


def weekly_activity(conn) -> list[dict]:
    """Per week: run miles, strength sessions and minutes, recovery sessions."""
    rows = conn.execute(
        """SELECT date(started_at, 'weekday 0', '-6 days') AS week,
                  ROUND(SUM(CASE WHEN kind='run' THEN distance_m ELSE 0 END) / ?, 1) AS run_miles,
                  SUM(kind='strength') AS strength_sessions,
                  ROUND(SUM(CASE WHEN kind='strength' THEN moving_time_s ELSE 0 END) / 60.0) AS strength_minutes,
                  SUM(kind='recovery') AS recovery_sessions
           FROM activities WHERE COALESCE(elapsed_time_s, moving_time_s) >= ?
           GROUP BY week ORDER BY week""",
        (METERS_PER_MILE, MIN_WORKOUT_SECONDS),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- plan lines ------------------------------------------------------------

def plan_line(goal: dict, actual: list[tuple[str, float]]) -> dict:
    """Straight line from start to target. start_value falls back to the first actual
    value on or after start_date. The target is target_value, or start + target_change."""
    start, target = _d(goal["start_date"]), _d(goal["target_date"])
    start_value = goal.get("start_value")
    if start_value is None:
        after = [v for d, v in actual if _d(d) >= start] or [v for _, v in actual[-1:]]
        start_value = after[0] if after else None
    if start_value is None:
        return {"start": None}
    target_value = goal.get("target_value")
    if target_value is None:
        target_value = round(start_value + goal.get("target_change", 0.0), 1)
    total = max((target - start).days, 1)

    def expected(on: date) -> float:
        frac = min(max((on - start).days / total, 0), 1)
        return start_value + (target_value - start_value) * frac

    latest = actual[-1] if actual else None
    status = None
    if latest:
        exp = expected(_d(latest[0]))
        gap = latest[1] - exp
        ahead = gap > 0 if goal["direction"] == "up" else gap < 0
        status = {"latest": round(latest[1], 1), "expected": round(exp, 1),
                  "gap": round(gap, 1), "ahead": ahead}
    return {"start": (start.isoformat(), round(start_value, 1)),
            "end": (target.isoformat(), target_value), "status": status}
