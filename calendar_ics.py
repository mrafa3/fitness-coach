"""Write the week's plan as an .ics calendar file.

  python calendar_ics.py            # writes ~/.fitness-coach/training.ics

Each session keeps a stable ID, so re-importing (or a calendar subscription)
updates events in place instead of duplicating them.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from coach import load_profile
from db import HOME, connect
from metrics import treadmill_mph

OUT = HOME / "training.ics"


def _esc(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode()); cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def _minutes(x) -> int:
    if x["kind"] == "run" and x["distance_mi"] and x["target_pace"]:
        return int(x["distance_mi"] * x["target_pace"]) + 10   # plus warm-up/cool-down
    if x["minutes"]:
        return x["minutes"]
    return 30


def _description(x) -> str:
    parts = []
    if x["kind"] == "run" and x["target_pace"]:
        mph = treadmill_mph(x["target_pace"])
        parts.append(f"Treadmill: {mph} mph, 1% incline "
                     f"(the 1% roughly matches outdoor effort; use 0% if you prefer)")
    if x["kind"] == "strength":
        focus = {"lower": "lower-body", "upper": "upper-body", "core": "core",
                 "full_body": "full-body"}.get(x["body_focus"], "full-body")
        parts.append(f"Pick any Nike Training Club {focus} workout around {x['minutes'] or 30} minutes.")
        if x["details"]:
            parts.append("No NTC today? Do this instead:\n" + x["details"])
    elif x["details"]:
        parts.append(x["details"])
    return "\n\n".join(parts)


def planned_rows(conn, weeks_back: int = 1, since=None):
    """Sessions from a week ago onward. gcal passes `since` (a date in the calendar's time
    zone) so its window matches how Google reads the events."""
    since = (since or (datetime.now() - timedelta(days=7 * weeks_back)).date()).isoformat()
    return conn.execute("SELECT * FROM planned_sessions WHERE day >= ? ORDER BY day", (since,)).fetchall()


def session_event(x, schedule: dict) -> dict:
    """One planned session as a calendar event. Shared by the .ics feed and Google Calendar."""
    tz = schedule.get("timezone", "UTC")
    weekend = datetime.fromisoformat(x["day"]).strftime("%a") in schedule["weekend_days"]
    hhmm = schedule["weekend_time"] if weekend else schedule["weekday_time"]
    start = datetime.fromisoformat(f'{x["day"]}T{hhmm}').replace(tzinfo=ZoneInfo(tz))
    minutes = _minutes(x) if weekend else min(_minutes(x), schedule["weekday_max_minutes"])
    title = x["title"] or x["kind"].title()
    if x["kind"] == "run" and x["distance_mi"]:
        title += f' · {x["distance_mi"]} mi'
        if x["target_pace"]:
            m, sec = divmod(round(x["target_pace"] * 60), 60)
            title += f" @ {m}:{sec:02d} · {treadmill_mph(x['target_pace'])} mph"
    elif x["minutes"]:
        title += f' · {x["minutes"]} min'
    return {"key": f'{x["day"]}-{x["kind"]}', "title": title, "description": _description(x),
            "start": start, "end": start + timedelta(minutes=minutes), "timezone": tz}


def build(weeks_back: int = 1) -> str:
    conn = connect()
    s = load_profile()["schedule"]
    rows = planned_rows(conn, weeks_back)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//fitness-coach//EN",
             "X-WR-CALNAME:Training", "CALSCALE:GREGORIAN"]
    for x in rows:
        e = session_event(x, s)
        lines += ["BEGIN:VEVENT",
                  f'UID:{e["key"]}@fitness-coach',
                  f"DTSTAMP:{stamp}",
                  f"DTSTART:{e['start'].astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"DTEND:{e['end'].astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"SUMMARY:{_esc(e['title'])}",
                  f"DESCRIPTION:{_esc(e['description'])}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT30M", f"DESCRIPTION:{_esc(e['title'])}", "END:VALARM",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    tmp = OUT.with_suffix(".ics.tmp")
    tmp.write_text("\r\n".join(_fold(l) for l in lines) + "\r\n", newline="")
    os.replace(tmp, OUT)  # atomic: a phone refreshing mid-write never gets an empty calendar
    print(f"Calendar: {len(rows)} sessions → {OUT}")
    return str(OUT)


if __name__ == "__main__":
    build()
