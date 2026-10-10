# Fitness Coach

A small local app that tracks two goals against a plan line and writes you a training plan every Sunday.

- **Lean mass.** 14-day rolling average of the Eufy scale's lean body mass. The target is a gain set in `goals.toml`.
- **6 miles in 60 minutes.** Every run of 1+ mile becomes a projected 6-mile time with Riegel's formula, `T2 = T1 × (6 / miles)^1.06`; the weekly best is the trend line.

## How data flows

```
EufyLife ─┐
iFit     ─┼─► Apple Health ─► Health Auto Export (iPhone) ── home Wi-Fi ──► receiver.py (Mac) ─► ~/.fitness-coach/workout.db
NTC      ─┘                     2 REST API automations, hourly                  token + LAN only
```

Every send covers the previous 7 days and imports are idempotent, so a send missed while the Mac sleeps is filled in by the next one. After each import the report rebuilds.

What the coach can see: workout type and length for everything, distance and pace for runs, and daily weight, lean mass and body fat. Nike Training Club doesn't pass workout titles or heart rate to Apple Health, so strength sessions are prescribed as a **focus and length** ("Lower body · 30 min") and you pick any matching NTC workout. Effort is judged from completion: sessions done, done at full length, and runs at the planned distance and pace.

## Files

| File | What it does |
|---|---|
| `receiver.py` | Listens for Health Auto Export on port 8765; checks the token and that the sender is on your network; rebuilds the report after each import |
| `health.py` | Parses and range-checks the Health Auto Export JSON, then saves it |
| `import_health.py` | Loads a Health Auto Export JSON file by hand (backfill) |
| `schema.sql`, `db.py` | The SQLite database in `~/.fitness-coach` (owner-only) |
| `goals.toml` | Your goals: metric, target, dates. Created from `goals.example.toml`; stays on your Mac |
| `metrics.py` | Workout classification, rolling averages, Riegel projections, plan lines |
| `coach.toml` | What the coach can't learn from data: schedule, equipment, guardrail limits. Created from `coach.example.toml`; stays on your Mac |
| `coach_prompt.md` | The coach's instructions. Plain markdown; edit to change its approach |
| `coach.py` | Scores last week, asks Claude for next week, validates and enforces guardrails, saves the plan |
| `calendar_ics.py` | Writes the plan to `training.ics` |
| `report.py` | Builds `report.html` (charts bundled in `vendor/`, no network requests); the receiver serves it to your phone at home |
| `run_weekly.py` | The Sunday job: plan, calendar, report, then Google Calendar if connected |
| `google_api.py`, `google_auth.py` | Google sign-in (browser + PKCE) and API calls, standard library only |
| `gcal.py` | Keeps the Training calendar in your Google account in step with the plan |
| `summary.py` | The weekly summary that goes in the "Week plan" calendar event |
| `check_public.py` | Refuses a git push that contains secrets, your data or your settings |
| `docs/` | The homepage and privacy policy Google asks for, published with GitHub Pages |
| `setup.sh` | One-time setup; `--uninstall` undoes it |

## Setup

Keep this folder in your home folder (`~/fitness-coach`), not Documents, Desktop, Downloads or iCloud, because macOS blocks background jobs from reading those.

You need:

1. **Python 3.11+.** `python3 --version`; if it says 3.9, `brew install python@3.12`.
2. **Claude Code, signed in with your Claude account.** `brew install --cask claude-code`, then `claude auth login`.
3. **Health Auto Export** on your iPhone with Premium ($6.99/year or $24.99 lifetime), which runs automations in the background. EufyLife, iFit and NTC each need their Apple Health sync turned on.

Then:

```bash
cd ~/fitness-coach && bash setup.sh
```

Setup creates a token, starts the receiver, schedules the Sunday coach, checks the receiver answers, and prints the exact Health Auto Export settings: two automations (Health Metrics, and Workouts version 2) with the URL, header, JSON, date range "Previous 7 Days", hourly. Allow Local Network access on the iPhone and incoming connections for Python on the Mac when asked. Open each automation and tap "Manual Export", then check `~/.fitness-coach/receiver.log` for "imported from" lines.

Sends only land when the iPhone is unlocked, on the same Wi-Fi, and the Mac is awake. "Wake for network access" (System Settings → Battery → Options) helps. The report and the coach both say when data has gone stale.

After data arrives, set your targets in `goals.toml` and run `./run.sh weekly` for your first plan.

**The report on your phone:** setup prints a `http://<your-mac>.local:8765/report?key=…` address. Open it in Safari on your iPhone and bookmark it. It loads on home Wi-Fi while the Mac is awake, behind its own read-only key (`~/.fitness-coach/report_token`). The page can't load or send anything over the network.

**Backfill (optional):** Health Auto Export → Manual Export → JSON with the same data types → `.venv/bin/python import_health.py <file>`.

## Weekly coach

Every Sunday at 8pm (or when the Mac next wakes), the coach:

1. **Scores last week.** Each planned session is matched to what Apple Health recorded (same day first, then a day early or late; each workout counts once): runs on distance and pace, strength and recovery on whether they ran their planned length.
2. **Builds context.** Goal status, last week's score, four weeks of workouts, the lean-mass trend, the workout types you actually do, and code-computed signals like "longest run hasn't grown in 3 weeks" or "no data from your iPhone in 3 days."
3. **Asks Claude for a plan.** Runs get distance, pace and treadmill mph; strength gets a body focus and minutes, plus a short fallback routine for days without NTC.
4. **Validates and enforces guardrails in code.** Every field is type- and range-checked. Weekly miles can rise at most 10% and the long run at most 1 mile; weekday sessions stay within 45 minutes and 4 mornings; there's never a third hard day in a row (a strength day becomes recovery first, then an easy run, the long run last). Over the mileage cap, the other runs are trimmed before the long run. A plan that breaks a limit goes back to Claude once, with the rejected plan; if it still does, the code clamps it and notes what changed.
5. **Retries if Claude can't be reached** (three tries over about 12 minutes, which covers Wi-Fi coming back after the Mac wakes). If it still fails, you get a macOS notification and the details are in `weekly.log`.
6. **Saves it**, writes the calendar, and puts "Week of …" at the top of the report with checkmarks as sessions get done.

The coach calls Claude Code in safe mode (no hooks, plugins, CLAUDE.md or memory) with every tool and MCP server disabled, in an empty temporary folder, with nothing saved and only a minimal environment. It can only read the weekly summary and reply with a plan, billed to your Claude plan. `python coach.py --show-context` prints exactly what it sends.

## Google Calendar (optional, recommended)

After each Sunday plan, the coach puts the week's sessions in a **Training** calendar in your Google account, so they show in Notion Calendar or anything else reading Google Calendar, on any device, whether or not the Mac is awake. Each week also gets an all-day **Week plan** event on Monday with the weekly summary: the focus, each session, where you stand on your goals, last week's score, and anything the coach's limits changed. It's marked as free time and has no reminder.

It asks Google for one permission, `calendar.app.created`: create its own calendar and manage events there. It can't see or change your other calendars, and it has no access to your email or anything else in your account. Every event it makes carries a private marker, and it only updates or removes marked events, so anything you add to the Training calendar is left alone. If you delete or move one of its sessions, it stays that way unless the coach later changes that session. If you delete the whole Training calendar, the Sunday job stops and tells you, rather than quietly making a new one.

**One-time setup:**

1. **Publish the two pages in `docs/`.** Google requires a homepage and privacy policy before it lets an app out of "Testing" (where sign-ins expire every 7 days). Put this project on GitHub (see "Putting this on GitHub"), then in the repo's **Settings → Pages**, choose **Deploy from a branch**, branch **main**, folder **/docs**, and Save. A minute later the pages are at `https://<you>.github.io/<repo>/` and `.../privacy.html`.
2. Go to https://console.cloud.google.com, signed in with the Google account whose calendar you use. Create a project, e.g. `fitness-coach`.
3. **APIs & Services → Library**: enable **Google Calendar API**.
4. **Google Auth Platform** → **Get started**: app name `fitness-coach`, your email as support and contact email, audience **External**. Create.
5. **Branding**: application home page `https://<you>.github.io/<repo>/`, privacy policy `https://<you>.github.io/<repo>/privacy.html`, and under **Authorized domains** add `<you>.github.io`. Leave the logo empty (a logo triggers Google's review). Save.
6. **Data Access → Add or remove scopes**: add `.../auth/calendar.app.created`. Save.
7. **Audience → Publish app → Confirm.** The status should read "In production".
8. **Clients → Create client → Desktop app**, name it anything, Create, then **Download JSON**, and move it into place:
   ```bash
   mv ~/Downloads/client_secret_*.json ~/.fitness-coach/google_client.json && chmod 600 ~/.fitness-coach/google_client.json
   ```
9. Sign in:
   ```bash
   cd ~/fitness-coach && .venv/bin/python google_auth.py
   ```
   Your browser opens Google's sign-in. You'll see "Google hasn't verified this app": that's expected for a personal app; choose **Advanced → Go to fitness-coach (unsafe)**, tick the calendar permission, and Continue. It then creates the Training calendar and adds the current plan.
10. In Notion Calendar, turn on the new **Training** calendar under your Google account if it isn't shown already.

From then on the Sunday job keeps it current. If that fails you get a Mac notification, and the details are in `weekly.log`. To disconnect, run `bash setup.sh --uninstall` (it revokes the Google sign-in), or remove fitness-coach at https://myaccount.google.com/connections and delete `~/.fitness-coach/google_token.json`. Network blips and Google rate limits are retried a few times before giving up until the next Sunday. Once this works, delete the iPhone subscribed calendar if you added one, so sessions don't show twice.

## Putting this on GitHub

Everything private lives in `~/.fitness-coach`, outside this folder: your data, the report, the keys, the Google sign-in and the logs. Inside the folder, `.gitignore` is an allowlist: git ignores everything except the code (`*.py`, `*.sh`, `*.example.toml`, `schema.sql`) and a short list of named docs and vendor files. So your settings (`coach.toml`, `goals.toml`), the files setup makes, and any stray export, report, backup, copy or notes file stay out whatever they're named. To add a new kind of file, add it to both `.gitignore` and `ALLOWED` in `check_public.py`.

`check_public.py` enforces the same allowlist before every push, and also refuses anything that looks like a key, a token, Health data, a database, a path from your Mac, one of your actual keys, or a commit with your real email, in files and in commit messages. It checks every commit GitHub doesn't have yet, so something committed and then deleted is still caught. `git push --no-verify` skips it, so don't use that here.

**First time, in this order:**

1. On GitHub, turn on **Settings → Emails → Keep my email addresses private**, and copy the `…@users.noreply.github.com` address it shows. (Commits carry an email, and it becomes public.)
2. In this folder:
   ```bash
   git init -b main
   git config user.email "<your noreply address>"
   bash setup.sh                 # now installs the push check as a git hook
   git add -A && git status      # review: only code and docs should be listed
   git commit -m "fitness-coach"
   python3 check_public.py       # should say: nothing secret or personal found
   ```
3. On GitHub, create a new **public** repository with no README, license or .gitignore (so it starts empty), then follow its "push an existing repository" commands (`git remote add origin …` and `git push -u origin main`). The check runs on that push.

Commit times include your time-zone offset. That's normal for git and can't be hidden without extra steps.

## Security notes

- The receiver accepts only `POST /health`, a do-nothing `GET /ping`, and the read-only calendar and report pages (each behind its own key), only from private addresses, only with the token in `~/.fitness-coach/receiver_token`, at most 25 MB and 60 seconds per connection, and 8 connections at once. Data and the token travel over plain HTTP on your home Wi-Fi, so anyone on that network who can watch traffic could read them.
- It listens on whatever network the Mac joins. On café, hotel or office Wi-Fi, other devices can't get data in without the token, but they could tie up the receiver. If that bothers you, stop it while away: `launchctl unload ~/Library/LaunchAgents/com.fitness-coach.receiver.plist` (and `load` to start it again).
- `bash setup.sh --uninstall` stops the receiver and the coach and deletes the token and keys. Turn off the automation in Health Auto Export too. Your data stays in `~/.fitness-coach` until you delete it.

## Notes

- Weigh in first thing in the morning; bioimpedance readings swing with hydration.
- Lean mass includes water and bone, so it's larger than the scale's muscle-mass number, but it moves with it. Revisit `target_change` after a few weeks of readings.
- The run projection is optimistic while your longest run is well short of 6 miles, so the report shows the longest run each week next to it.
- Logs: `~/.fitness-coach/receiver.log`, `weekly.log`, and `launchd.log` (startup errors).
- After editing the code, restart the receiver: `launchctl kickstart -k gui/$(id -u)/com.fitness-coach.receiver`.
