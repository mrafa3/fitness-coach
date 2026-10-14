"""Parse Health Auto Export JSON and upsert it into the database.

Expected shape (https://help.healthyapps.dev/en/health-auto-export/export-format/):
  {"data": {"metrics": [{"name": ..., "units": ..., "data": [{"date": ..., "qty": ...}]}],
            "workouts": [{"id", "name", "start", "end", "duration", "distance": {"qty","units"},
                          "activeEnergyBurned": {...}, "isIndoor", "location"}]}}

Everything here is untrusted input: unknown metrics are ignored, every number is
range-checked, and bad entries are skipped and counted rather than stored.
"""
import json
import math
import re
from datetime import datetime

from db import start_key, workout_id
from metrics import classify

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d")
TO_LB = {"lb": 1.0, "lbs": 1.0, "kg": 2.20462, "g": 0.00220462}
TO_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}
TO_KCAL = {"kcal": 1.0, "cal": 1.0, "kj": 0.239006}
TO_MPH = {"mi/hr": 1.0, "mph": 1.0, "mi/h": 1.0, "km/hr": 0.621371, "km/h": 0.621371, "kph": 0.621371,
          "m/s": 2.23694}


def _norm(name) -> str:
    return re.sub(r"[^a-z]", "", str(name).lower())


def _metric_column(name) -> str | None:
    n = _norm(name)
    if "lean" in n:
        return "lean_mass_lb"
    if "bodyfat" in n:
        return "body_fat_pct"
    if n in ("weightbodymass", "bodymass", "weight"):
        return "weight_lb"
    return None


def _finite(v, lo: float, hi: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) and lo <= v <= hi else None


def _when(s) -> datetime | None:
    """Local wall-clock time as recorded (the offset is the phone's at that moment)."""
    t = _instant(s)
    return t.replace(tzinfo=None) if t else None


def _instant(s) -> datetime | None:
    """The time as parsed, keeping its UTC offset when the sender included one."""
    if not isinstance(s, str):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _quantity(obj, table: dict) -> float | None:
    """{"qty": 3.1, "units": "mi"} -> value in the table's base unit."""
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return _finite(obj, 0, 1e7)
    if not isinstance(obj, dict):
        return None
    factor = table.get(str(obj.get("units", "")).strip().lower())
    q = _finite(obj.get("qty"), 0, 1e7)
    return q * factor if (q is not None and factor) else None


def _metric_value(col: str, qty, units) -> float | None:
    u = str(units or "").strip().lower()
    if col in ("weight_lb", "lean_mass_lb"):
        q = _finite(qty, 0, 2000)
        factor = TO_LB.get(u, 1.0 if u == "" else None)
        if q is None or factor is None:
            return None
        return _finite(round(q * factor, 2), 40 if col == "weight_lb" else 20, 600)
    # body fat: Apple stores a fraction; exports may give 0.22 or 22
    q = _finite(qty, 0, 100)
    if q is None:
        return None
    return _finite(round(q * 100 if q <= 1 else q, 1), 2, 75)


def ingest(conn, payload) -> dict:
    """Upsert one Health Auto Export payload. Returns counts; never raises on bad entries."""
    now = datetime.now().isoformat(timespec="seconds")
    counts = {"weigh_in_values": 0, "workouts": 0, "skipped": 0, "ignored_metrics": 0, "excluded": 0}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Not a Health Auto Export payload (no 'data' object)")

    metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else []
    workouts = data.get("workouts") if isinstance(data.get("workouts"), list) else []
    for m in metrics:
        if not isinstance(m, dict):
            counts["skipped"] += 1
            continue
        col = _metric_column(m.get("name"))
        if col is None:
            counts["ignored_metrics"] += 1
            continue
        for p in m.get("data") if isinstance(m.get("data"), list) else []:
            when = _when(p.get("date")) if isinstance(p, dict) else None
            val = _metric_value(col, p.get("qty"), m.get("units")) if when else None
            if val is None:
                counts["skipped"] += 1
                continue
            # col comes from a fixed whitelist above, never from the payload
            conn.execute(
                f"""INSERT INTO weigh_ins (day, {col}, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET {col}=excluded.{col}, updated_at=excluded.updated_at""",
                (when.date().isoformat(), val, now))
            counts["weigh_in_values"] += 1

    for w in workouts:
        if not isinstance(w, dict):
            counts["skipped"] += 1
            continue
        start = _when(w.get("start"))
        name = w.get("name") if isinstance(w.get("name"), str) else None
        duration = _finite(w.get("duration"), 0, 24 * 3600)
        if not (start and name and duration is not None):
            counts["skipped"] += 1
            continue
        name = name.strip()[:80]
        distance = _quantity(w.get("distance"), TO_M)
        distance = distance if distance is not None and distance <= 200_000 else None
        energy = _quantity(w.get("activeEnergyBurned"), TO_KCAL)
        energy = energy if energy is not None and energy <= 20_000 else None
        indoor = w.get("isIndoor") is True or str(w.get("location", "")).lower() == "indoor"
        end = _when(w.get("end"))
        elapsed = int((end - start).total_seconds()) if end and 0 <= (end - start).total_seconds() <= 24 * 3600 else None
        speed = _quantity(w.get("speed") or w.get("avgSpeed"), TO_MPH)
        if (speed is None or not 0.5 <= speed <= 20) and distance and duration:
            speed = distance / 1609.344 / (duration / 3600)  # derive it if the app didn't send one
        speed = round(speed, 2) if speed is not None and 0.5 <= speed <= 20 else None
        src = w.get("source")
        src = src.get("name") if isinstance(src, dict) else src
        src = src.strip()[:60] if isinstance(src, str) else None
        # Health Auto Export gives the same workout a new "id" on every send, so a workout is
        # identified by its type, its start as a UTC instant (so a phone in another time zone
        # doesn't make it look new) and the recording app
        wid = workout_id(name, start_key(start, _instant(w.get("start"))), src)
        if conn.execute("SELECT 1 FROM excluded_workouts WHERE id=?", (wid,)).fetchone():
            counts["excluded"] += 1  # you marked this one as not yours (exclude.py)
            continue
        conn.execute(
            """INSERT INTO activities (id, started_at, sport_type, kind, distance_m, moving_time_s,
                                       elapsed_time_s, source, avg_speed_mph, trainer, energy_kcal, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at, sport_type=excluded.sport_type,
                   kind=excluded.kind, distance_m=excluded.distance_m, moving_time_s=excluded.moving_time_s,
                   elapsed_time_s=excluded.elapsed_time_s, source=excluded.source,
                   avg_speed_mph=excluded.avg_speed_mph,
                   trainer=excluded.trainer, energy_kcal=excluded.energy_kcal, updated_at=excluded.updated_at""",
            (wid, start.isoformat(timespec="seconds"), name, classify(name), distance,
             int(duration), elapsed, src, speed if classify(name) == "run" else None, int(indoor), energy, now))
        counts["workouts"] += 1

    # also catches a send that raced an exclusion made at the same moment
    conn.execute("DELETE FROM activities WHERE id IN (SELECT id FROM excluded_workouts)")
    if counts["weigh_in_values"] or counts["workouts"]:  # an empty send isn't "data arrived"
        conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('health', ?)", (now,))
    conn.commit()
    return counts


def loads(text: str):
    def reject(c):
        raise ValueError(f"Payload contained {c}")
    return json.loads(text, parse_constant=reject)
