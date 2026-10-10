"""One-time Google sign-in, then a first sync of the Training calendar.

  .venv/bin/python google_auth.py

Before running, download your OAuth client file from Google Cloud (Desktop app) and
put it at ~/.fitness-coach/google_client.json (see README: Google Calendar).
Run it again any time to re-authorize; revoke access at
https://myaccount.google.com/connections.
"""
import sys

import gcal
import google_api as g

if __name__ == "__main__":
    try:
        if g.CLIENT_FILE.exists():
            g.CLIENT_FILE.chmod(0o600)
        g.sign_in()
        print("Signed in to Google.")
        gcal.sync(create=True)
        gcal.calendar_id()  # the same check the Sunday job makes, so a problem shows up now
        print("\nDone. The Training calendar should appear in Google Calendar and Notion Calendar shortly.")
    except g.GoogleError as e:
        sys.exit(f"\n{e}")
