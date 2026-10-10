"""Keep a "Training" calendar in your Google account in step with the plan.

google_auth.py creates the calendar (the only kind of calendar this app can touch).
Each sync, for every planned session from a week ago onward, it adds the event or
updates it when the plan for it changed, and removes this app's events that are no
longer in the plan. Each week also gets one all-day "Week plan" event on its Monday
with the weekly summary (summary.py). Every event it makes carries a private marker, and it only ever
updates or removes marked events, so anything you add to the calendar is left alone.
A session you delete or move yourself stays that way until the coach changes it.
"""
import hashlib
import json
import re
import tomllib
import urllib.parse
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta
from pathlib import Path

import google_api as g
from calendar_ics import planned_rows, session_event
from summary import frozen_summary
from db import HOME, connect

API = "https://www.googleapis.com/calendar/v3"
STATE_FILE = HOME / "google_state.json"
ID_PREFIX = "fc"  # every event this app makes: "fc" + 40 hex characters
OWN_ID = re.compile(r"fc[0-9a-f]{40}")
MARKER = {"private": {"fitnessCoach": "1"}}


def _settings() -> dict:
    cfg = tomllib.loads((Path(__file__).parent / "coach.toml").read_text())
    return cfg.get("google", {}) | {"timezone": cfg["schedule"].get("timezone", "UTC")}


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def event_id(key: str) -> str:
    """Stable ID per session (day + kind). Google allows 0-9 and a-v; hex fits."""
    return ID_PREFIX + hashlib.sha1(key.encode()).hexdigest()


def calendar_id(create: bool = False) -> str:
    """The Training calendar's ID. Only google_auth.py creates it (create=True); the Sunday
    job won't silently recreate a calendar you deleted."""
    st = _state()
    if st.get("calendar_id"):
        status, body = g.call("GET", f"{API}/calendars/{urllib.parse.quote(st['calendar_id'], safe='')}")
        if status == 200:
            return st["calendar_id"]
        if status not in (404, 410):
            raise g.fail("Checking the Training calendar", status, body)
        if not create:
            raise g.GoogleError("The Training calendar is gone from Google Calendar. To bring it back, run "
                                ".venv/bin/python google_auth.py. To stop syncing, delete "
                                "~/.fitness-coach/google_token.json.")
    elif not create:
        raise g.GoogleError("No Training calendar yet. Run: .venv/bin/python google_auth.py")
    s = _settings()
    status, body = g.call("POST", f"{API}/calendars", {"summary": s.get("calendar_name", "Training"),
                                                        "timeZone": s["timezone"]}, idempotent=False)
    if status != 200 or not body.get("id"):
        raise g.fail("Creating the Training calendar", status, body)
    g.write_private(STATE_FILE, {"calendar_id": body["id"], "synced": {}})
    return body["id"]


def _digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def sync(conn=None, create: bool = False) -> dict:
    conn = conn or connect()
    cid = calendar_id(create)
    cal = urllib.parse.quote(cid, safe="")
    sched = tomllib.loads((Path(__file__).parent / "coach.toml").read_text())["schedule"]
    st = _state()
    synced: dict = st.get("synced", {}) if st.get("calendar_id") == cid else {}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    errors: list[str] = []
    wanted = set()
    finished = False
    since = datetime.now(ZoneInfo(sched.get("timezone", "UTC"))) - timedelta(days=7)
    since = since.replace(hour=0, minute=0, second=0, microsecond=0)

    def upsert(eid: str, body: dict, label: str) -> None:
        wanted.add(eid)
        body = {"id": eid, "status": "confirmed", "extendedProperties": MARKER} | body
        digest = _digest(body)
        if synced.get(eid) == digest:  # plan unchanged: leave it, including any edit you made
            counts["unchanged"] += 1
            return
        url = f"{API}/calendars/{cal}/events/{urllib.parse.quote(eid, safe='')}"
        status, resp = g.call("PUT", url, body)  # update (also restores one the coach changed after you deleted it)
        if status == 404:
            status, resp = g.call("POST", f"{API}/calendars/{cal}/events", body)
            if status == 409:  # the ID is still reserved by a purged event: one more update try
                status, resp = g.call("PUT", url, body)
            kind = "created"
        else:
            kind = "updated"
        if status == 200:
            counts[kind] += 1
            synced[eid] = digest
        else:
            errors.append(f"{label}: {g.fail('', status, resp)}")

    try:
        for x in planned_rows(conn, since=since.date()):
            e = session_event(x, sched)
            upsert(event_id(e["key"]), {
                "summary": e["title"], "description": e["description"],
                "start": {"dateTime": e["start"].isoformat(), "timeZone": e["timezone"]},
                "end": {"dateTime": e["end"].isoformat(), "timeZone": e["timezone"]},
                "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}},
                f"{x['day']} {x['kind']}")

        # one all-day "Week plan" event per plan in the same window, on its Monday.
        # Shown as free time and with no reminder, so it never blocks your day.
        for row in conn.execute("SELECT week_start FROM coach_plans WHERE week_start >= ? ORDER BY week_start",
                                (since.date().isoformat(),)).fetchall():
            ws = date.fromisoformat(row["week_start"])
            s = frozen_summary(conn, ws)
            if s:
                upsert(event_id(f"{ws.isoformat()}-summary"), {
                    "summary": s["title"], "description": s["description"],
                    "start": {"date": ws.isoformat()}, "end": {"date": (ws + timedelta(days=1)).isoformat()},
                    "transparency": "transparent", "reminders": {"useDefault": False, "overrides": []}},
                    f"week plan {ws}")

        # remove this app's events that the plan no longer has (same window as above).
        # Only events carrying this app's private marker AND its ID format are touched.
        page = None
        while True:
            params = {"timeMin": since.isoformat(), "singleEvents": "true", "showDeleted": "false",
                      "maxResults": "250", "privateExtendedProperty": "fitnessCoach=1",
                      "fields": "items(id),nextPageToken"}
            if page:
                params["pageToken"] = page
            status, resp = g.call("GET", f"{API}/calendars/{cal}/events", params=params)
            if status != 200:
                errors.append(f"listing events: {g.fail('', status, resp)}")
                break
            for item in resp.get("items", []):
                iid = item.get("id", "")
                if OWN_ID.fullmatch(iid) and iid not in wanted:
                    stt, r = g.call("DELETE", f"{API}/calendars/{cal}/events/{urllib.parse.quote(iid, safe='')}")
                    if stt in (200, 204):
                        counts["deleted"] += 1
                        synced.pop(iid, None)
                    elif stt not in (404, 410):
                        errors.append(f"removing an old session: {g.fail('', stt, r)}")
            page = resp.get("nextPageToken")
            if not page:
                break
        finished = True
    finally:
        # save progress even after a failure, so a session you deleted isn't re-added next time.
        # Forget past sessions only after a complete pass (otherwise "wanted" is partial).
        if finished:
            synced = {k: v for k, v in synced.items() if k in wanted}
        g.write_private(STATE_FILE, {"calendar_id": cid, "synced": synced})
    print(f"Google Calendar: {counts['created']} added, {counts['updated']} updated, "
          f"{counts['unchanged']} unchanged, {counts['deleted']} removed.")
    if errors:
        raise g.GoogleError("Some calendar changes didn't go through: " + "; ".join(errors[:5]))
    return counts


if __name__ == "__main__":
    sync()
