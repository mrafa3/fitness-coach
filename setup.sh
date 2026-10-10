#!/bin/bash
# One-time setup for fitness-coach on a Mac. Run from this folder:  bash setup.sh
# Safe to re-run; each step skips what's already done. Nothing here uses sudo or
# downloads anything. It touches only this folder, ~/.fitness-coach, and two files
# in ~/Library/LaunchAgents. Undo it all with:  bash setup.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd -P)"
ME="${USER:-$(id -un)}"
DATA="$HOME/.fitness-coach"
AGENTS="$HOME/Library/LaunchAgents"
say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
PORT=8765

if [ "${1:-}" = "--uninstall" ]; then
  for job in receiver weekly daily; do
    launchctl unload "$AGENTS/com.fitness-coach.$job.plist" 2>/dev/null || true
    rm -f "$AGENTS/com.fitness-coach.$job.plist"
  done
  google_msg=""
  if [ -f "$DATA/google_token.json" ]; then
    # cancel the Google sign-in at Google's end; keep the token if that didn't work
    if [ -x .venv/bin/python ] && .venv/bin/python google_api.py --revoke; then
      rm -f "$DATA/google_token.json" "$DATA/google_client.json"
      google_msg="Google sign-in revoked. The Training calendar stays in Google Calendar until you delete it there."
    else
      google_msg="Couldn't reach Google to revoke its sign-in. Remove fitness-coach at https://myaccount.google.com/connections, then delete $DATA/google_token.json."
    fi
  else
    rm -f "$DATA/google_client.json"
  fi
  # google_state.json is kept, so reinstalling reuses the same Training calendar
  rm -f "$DATA/receiver_token" "$DATA/calendar_token" "$DATA/report_token" .claude_path run.sh
  echo "Stopped the receiver and the Sunday coach, and deleted the upload token and the calendar and report keys."
  echo "On your iPhone, also turn off the Health Auto Export automations, delete the report"
  echo "bookmark, and delete the Training subscribed calendar if you added one."
  if [ -n "$google_msg" ]; then echo "$google_msg"; fi
  echo "Your training data is still in $DATA; delete that folder yourself if you want it gone."
  exit 0
fi

# macOS blocks background jobs from reading these folders, so scheduled runs would silently fail.
case "$DIR/" in
  "$HOME/Documents/"*|"$HOME/Desktop/"*|"$HOME/Downloads/"*|"$HOME/Library/Mobile Documents/"*|"$HOME/Library/CloudStorage/"*|/Volumes/*)
    echo "This folder is in a location macOS blocks for scheduled jobs:"
    echo "    $DIR"
    echo "Move it to your home folder and run setup from there:"
    echo "    mv \"$DIR\" ~/ && cd ~/$(basename "$DIR") && bash setup.sh"
    exit 1 ;;
esac

say "1/4  Python environment"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Needs Python 3.11 or newer (macOS ships 3.9). Install it with:  brew install python@3.12"
  echo "Then run this script again."
  exit 1
fi
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  echo "Replacing an old .venv built on an older Python."
  rm -rf .venv
fi
[ -d .venv ] || "$PY" -m venv --without-pip .venv   # standard library only; nothing to install
echo "Using $(.venv/bin/python --version)"
# python.org builds ship without HTTPS certificates until you run their installer
if ! .venv/bin/python - <<'PYCHECK' 2>/dev/null
import ssl, urllib.request, urllib.error
try:
    urllib.request.urlopen("https://oauth2.googleapis.com/", timeout=10)
except urllib.error.HTTPError:
    pass                      # any HTTP answer means HTTPS works
except urllib.error.URLError as e:
    raise SystemExit(1 if isinstance(e.reason, ssl.SSLCertVerificationError) else 0)
PYCHECK
then
  echo "This Python can't verify HTTPS certificates, so the Google features won't work."
  echo "If it's from python.org, run 'Install Certificates.command' in /Applications/Python 3.x,"
  echo "or install Homebrew's:  brew install python@3.12   then run this script again."
fi
mkdir -p "$DATA" && chmod 700 "$DATA"
# your settings: start from the examples, and never overwrite your edits
for f in coach goals; do
  if [ ! -f "$f.toml" ]; then cp "$f.example.toml" "$f.toml" && echo "Created $f.toml from the example; edit it to suit you."; fi
done
# in a git repo, check every push for secrets and personal data (see check_public.py)
if [ -d .git ]; then
  HOOK=.git/hooks/pre-push
  if [ ! -e "$HOOK" ] || grep -q "check_public.py" "$HOOK"; then
    # shellcheck disable=SC2016  # the hook's \$ expressions are meant to run later, not now
    printf '%s\n' '#!/bin/sh' 'cd "$(git rev-parse --show-toplevel)" || exit 1' \
      'PY=.venv/bin/python; [ -x "$PY" ] || PY=python3' 'exec "$PY" check_public.py --pre-push "$@"' > "$HOOK"
    chmod 755 "$HOOK"
    echo "Every git push is now checked for secrets and personal data."
  else
    echo "There's already a pre-push hook, so check_public.py wasn't installed. Run it yourself before pushing."
  fi
else
  echo "Not a git repo yet. Before putting this on GitHub, run  git init -b main  and then this"
  echo "script again, so every push gets checked for secrets and personal data."
fi

say "2/4  Claude Code (the coach runs on your Claude plan through it)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v claude >/dev/null; then
  echo "Claude Code isn't installed. Install it with one of these (official commands from"
  echo "https://code.claude.com/docs/en/setup), then run this script again:"
  echo "    brew install --cask claude-code"
  echo "    curl -fsSL https://claude.ai/install.sh | bash"
  exit 1
fi
CLAUDE="$(command -v claude)"
echo "Claude Code: $CLAUDE"
# same minimal environment the weekly coach uses, so this check means the same thing
METHOD="$(env -i HOME="$HOME" USER="$ME" LOGNAME="$ME" PATH="$PATH" LANG="${LANG:-en_US.UTF-8}" TMPDIR="${TMPDIR:-/tmp}" SHELL="${SHELL:-/bin/zsh}" \
          "$CLAUDE" auth status 2>/dev/null \
          | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin).get("authMethod",""))' 2>/dev/null || true)"
if [ "$METHOD" != "claude.ai" ]; then
  echo "Claude Code isn't signed in with your Claude account (found: ${METHOD:-none})."
  echo "Run:  claude auth login   and choose your Claude account (not Console), then run this script again."
  exit 1
fi
echo "signed in with your Claude account"

say "3/4  Keys (upload token for your iPhone, plus read-only keys for the calendar and report)"
short() { [ "$( { tr -d '[:space:]' < "$1"; } 2>/dev/null | wc -c)" -lt 32 ]; }
if short "$DATA/receiver_token"; then
  (umask 077 && .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$DATA/receiver_token")
fi
if short "$DATA/calendar_token" || [ "${1:-}" = "--rotate-calendar-key" ]; then   # separate, read-only key
  [ "${1:-}" = "--rotate-calendar-key" ] && echo "Making a new calendar key; update the subscription on your iPhone."
  (umask 077 && .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$DATA/calendar_token")
fi
if short "$DATA/report_token" || [ "${1:-}" = "--rotate-report-key" ]; then   # separate, read-only key
  [ "${1:-}" = "--rotate-report-key" ] && echo "Making a new report key; update the bookmark on your iPhone."
  (umask 077 && .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$DATA/report_token")
fi
chmod 600 "$DATA/receiver_token" "$DATA/calendar_token" "$DATA/report_token"
echo "saved in $DATA (receiver_token, calendar_token, report_token)"

say "4/4  Start the receiver and schedule the coach for Sundays at 8pm"
# Save the exact Claude Code path setup checked, so the scheduled job uses the same one.
printf '%s\n' "$CLAUDE" > .claude_path
cat > run.sh <<'EOF'
#!/bin/bash
# Called by launchd. Usage: run.sh receiver|weekly
set -euo pipefail
case "${1:-}" in receiver|weekly) ;; *) echo "usage: run.sh receiver|weekly" >&2; exit 2 ;; esac
mkdir -p "$HOME/.fitness-coach"
exec >> "$HOME/.fitness-coach/$1.log" 2>&1
echo "--- $(date) $1"
cd "$(dirname "$0")"
if [ "$1" = receiver ]; then exec .venv/bin/python receiver.py; fi
FC_CLAUDE="$(cat .claude_path)"
FC_CLAUDE_DIR="$(dirname "$FC_CLAUDE")"
export FC_CLAUDE
export PATH="$FC_CLAUDE_DIR:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
exec .venv/bin/python run_weekly.py
EOF
chmod 700 run.sh
bash -n run.sh
mkdir -p "$AGENTS"
xml() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }
XDIR="$(xml "$DIR")"; XDATA="$(xml "$DATA")"
plist() {  # job name, schedule XML (keys after ProgramArguments)
  cat > "$AGENTS/com.fitness-coach.$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.fitness-coach.$1</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$XDIR/run.sh</string><string>$1</string></array>
  $2
  <key>StandardOutPath</key><string>$XDATA/launchd.log</string>
  <key>StandardErrorPath</key><string>$XDATA/launchd.log</string>
</dict></plist>
EOF
  plutil -lint "$AGENTS/com.fitness-coach.$1.plist" >/dev/null
  launchctl unload "$AGENTS/com.fitness-coach.$1.plist" 2>/dev/null || true
  launchctl load "$AGENTS/com.fitness-coach.$1.plist"
}
# an older version had a daily Strava job; remove it if present
launchctl unload "$AGENTS/com.fitness-coach.daily.plist" 2>/dev/null || true
rm -f "$AGENTS/com.fitness-coach.daily.plist"
plist receiver "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>"
plist weekly "<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>"
echo "Coach scheduled. If the Mac is asleep on Sunday at 8pm, it runs when it wakes."
echo "Logs: $DATA/receiver.log, weekly.log, and launchd.log (startup errors)"
# one test notification now, so macOS asks permission while you're here rather than
# silently dropping a failure alert later
osascript -e 'display notification "Setup finished. You will see alerts like this if a weekly plan fails." with title "Fitness Coach"' || true
echo "If macOS asks whether to allow notifications (from Script Editor), allow them."

# Check the receiver answers. /ping touches nothing, so this can't fake "data arrived".
sleep 2
CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/ping" || true)"
if [ "$CODE" = 200 ]; then
  echo "Receiver is running."
else
  echo "The receiver didn't answer (got: ${CODE:-nothing}). See $DATA/receiver.log and $DATA/launchd.log."
  exit 1
fi

HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s).local"
say "Done. Now set up Health Auto Export on your iPhone (Premium needed for background sync):"
cat <<EOF
  Make two automations (Automations → New Automation → REST API), both with:
    URL:          http://$HOST:$PORT/health
    Headers:      Authorization = Bearer $(cat "$DATA/receiver_token")
    Format:       JSON
    Date range:   Previous 7 Days   (so a missed send is filled in by the next one)
    Sync cadence: every 1 hour
  1) Data type Health Metrics: Weight & Body Mass, Lean Body Mass, Body Fat Percentage
     (aggregation: Days)
  2) Data type Workouts, export version 2

  When iOS asks to let Health Auto Export find devices on your local network, tap Allow.
  If macOS asks whether Python may accept incoming network connections, click Allow.

  Test it: open each automation and tap "Manual Export", then on this Mac run
      tail ~/.fitness-coach/receiver.log
  and look for "imported from 192.168..." lines.

  Sends only land when your iPhone is unlocked, on the same Wi-Fi, and the Mac is awake.
  To let the Mac receive while its screen is off, turn on System Settings → Battery →
  Options → "Wake for network access".

Your charts report, on your iPhone at home: open this in Safari and bookmark it
(or Share → Add to Home Screen):
    http://$HOST:$PORT/report?key=$(cat "$DATA/report_token")
  It loads on home Wi-Fi while the Mac is awake. The address is plain HTTP, so only open it
  at home; to make a new key:  bash setup.sh --rotate-report-key

Your training calendar: connect Google Calendar (README: "Google Calendar") so the plan
shows in Notion Calendar and anywhere else you use Google Calendar.
  (Without Google, you can subscribe on the iPhone instead: Settings → Apps → Calendar →
  Calendar Accounts → Add Account → Other → Add Subscribed Calendar →
  http://$HOST:$PORT/training.ics?key=$(cat "$DATA/calendar_token")  It refreshes only on
  home Wi-Fi while the Mac is awake. New key:  bash setup.sh --rotate-calendar-key)

Then:
  1. Set your targets and dates in goals.toml.
  2. Once data arrives (~/.fitness-coach/report.html shows "Last data from iPhone"),
     make your first plan now instead of waiting for Sunday:   ./run.sh weekly
     If Google Calendar is connected, it shows up there right away.
  Optional backfill: in Health Auto Export, Manual Export → JSON → same data types, then
     .venv/bin/python import_health.py <file.json>
EOF
