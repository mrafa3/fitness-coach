"""Minimal Google OAuth + API client (standard library only).

Sign-in is the "installed app" flow: your browser opens Google's consent page, Google
redirects back to a one-time listener on 127.0.0.1, and the code is exchanged (with
PKCE) for a refresh token. It asks for one permission only:

  calendar.app.created   create its own calendar(s) and manage events on them.
                         It can't see or change your other calendars, and it has
                         no access to your email or anything else in your account.

Files in ~/.fitness-coach (owner-only):
  google_client.json     the OAuth client you download from Google Cloud
  google_token.json      the refresh token, written at sign-in
"""
import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from db import HOME

CLIENT_FILE = HOME / "google_client.json"
TOKEN_FILE = HOME / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 30


class GoogleError(RuntimeError):
    pass


def configured() -> bool:
    """True once you've started connecting Google; a missing piece then fails loudly."""
    return CLIENT_FILE.exists() or TOKEN_FILE.exists()


def _client() -> tuple[str, str]:
    try:
        data = json.loads(CLIENT_FILE.read_text())
        c = data.get("installed") or data.get("web") or {}
        return c["client_id"], c["client_secret"]
    except (OSError, ValueError, KeyError):
        raise GoogleError(f"{CLIENT_FILE} is missing or isn't a Google OAuth client file. "
                          "Download it from Google Cloud → Clients → your Desktop client.")


def write_private(path, obj) -> None:
    """Write JSON readable only by you, atomically."""
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.unlink()  # never reuse a leftover temp file with other permissions
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAITS = (2, 4, 8)


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None,
          idempotent: bool = True) -> tuple[int, dict]:
    """One request, retried a few times on network errors, rate limits and Google 5xx.
    idempotent=False (creating the calendar, exchanging the sign-in code) is
    retried only when Google surely never got it: the connection couldn't be opened, or Google
    answered 429 "slow down". A timeout or 5xx might mean it went through, so it isn't repeated."""
    import http.client
    import ssl
    import time
    last = None
    maybe_sent = False
    for wait in (0, *RETRY_WAITS):
        if wait:
            time.sleep(wait)
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            body, status = e.read(), e.code
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise GoogleError("Python can't verify Google's HTTPS certificate. If you installed Python "
                                  "from python.org, run its 'Install Certificates.command' "
                                  "(in /Applications/Python 3.x), or use Homebrew's python@3.12.")
            last = f"Couldn't reach Google ({e.reason})"
            if not idempotent and isinstance(e.reason, TimeoutError):
                maybe_sent = True
                break  # may have been sent; don't risk doing it twice
            continue
        except (OSError, http.client.HTTPException) as e:  # timeouts, resets, dropped connections
            last = f"Connection to Google failed ({type(e).__name__})"
            if not idempotent:
                maybe_sent = True
                break
            continue
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}
        reason = ""
        if isinstance(parsed.get("error"), dict):
            errs = parsed["error"].get("errors") or [{}]
            reason = errs[0].get("reason", "") if isinstance(errs[0], dict) else ""
        rate_limited = status == 429 or (status == 403 and reason in ("rateLimitExceeded", "userRateLimitExceeded"))
        if rate_limited or (idempotent and status in RETRY_STATUS):
            last = f"Google answered {status}"
            continue
        return status, parsed
    if maybe_sent:
        raise GoogleError(f"{last}. It may or may not have gone through, so it wasn't repeated; "
                          "it will try again next Sunday.")
    raise GoogleError(f"{last}, even after retrying. Check the internet connection; it will try again next Sunday.")


def _token_request(fields: dict) -> dict:
    # refreshing is safe to repeat; a sign-in code works only once
    status, body = _http("POST", TOKEN_URL, urllib.parse.urlencode(fields).encode(),
                         {"Content-Type": "application/x-www-form-urlencoded"},
                         idempotent=fields.get("grant_type") == "refresh_token")
    if status != 200:
        # error codes only; never echo tokens or secrets
        err = body.get("error", status)
        if err == "invalid_grant":
            raise GoogleError("Google sign-in has expired or was revoked. Run: .venv/bin/python google_auth.py")
        raise GoogleError(f"Google token request failed: {err}")
    return body


def sign_in(open_browser=True) -> None:
    """Run the consent flow in your browser and save the sign-in."""
    import webbrowser
    client_id, client_secret = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = 10  # an idle connection can't block the real redirect

        def log_message(self, *args):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"State mismatch. Close this tab and try again.")
                return
            result["code"] = q.get("code", [""])[0]
            result["error"] = q.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if result["code"] else "Sign-in was cancelled."
            self.wfile.write(f"<p style='font:16px system-ui'>{msg}</p>".encode())

    server = HTTPServer(("127.0.0.1", 0), Callback)  # loopback only, random free port
    server.timeout = 15
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent"})
    print("Opening Google sign-in in your browser. If it doesn't open, paste this address:\n\n  " + url + "\n")
    if open_browser:
        webbrowser.open(url)
    import time
    deadline = time.monotonic() + 300
    while "code" not in result:  # ignore stray requests (e.g. favicon) until Google redirects back
        server.handle_request()
        if time.monotonic() > deadline:
            server.server_close()
            raise GoogleError("Timed out waiting for Google sign-in (5 minutes).")
    server.server_close()
    if not result["code"]:
        raise GoogleError(f"Sign-in didn't complete ({result.get('error') or 'no code'}).")

    tokens = _token_request({"code": result["code"], "client_id": client_id, "client_secret": client_secret,
                             "redirect_uri": redirect, "grant_type": "authorization_code",
                             "code_verifier": verifier})
    if not tokens.get("refresh_token"):
        raise GoogleError("Google didn't return a refresh token. Remove the app at "
                          "https://myaccount.google.com/connections and sign in again.")
    granted = set(tokens.get("scope", "").split())
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        _revoke_token(tokens["refresh_token"])  # don't leave a half-permitted sign-in active
        raise GoogleError("Some permissions were unchecked on Google's consent page: "
                          + ", ".join(m.rsplit("/", 1)[-1] for m in missing) + ". Sign in again and tick the calendar box.")
    write_private(TOKEN_FILE, {"refresh_token": tokens["refresh_token"], "scopes": sorted(granted)})


_access: dict = {}


def access_token() -> str:
    if _access.get("token"):
        return _access["token"]
    try:
        saved = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        raise GoogleError("Not signed in to Google. Run: .venv/bin/python google_auth.py")
    client_id, client_secret = _client()
    tok = _token_request({"refresh_token": saved["refresh_token"], "client_id": client_id,
                          "client_secret": client_secret, "grant_type": "refresh_token"})
    _access["token"] = tok["access_token"]
    return _access["token"]


def call(method: str, url: str, body: dict | None = None, params: dict | None = None,
         idempotent: bool = True) -> tuple[int, dict]:
    """Authorized JSON request. Returns (status, json); the caller decides what's an error."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {access_token()}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return _http(method, url, data, headers, idempotent)


def fail(what: str, status: int, body: dict) -> GoogleError:
    msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
    return GoogleError(f"{what} failed ({status}): {str(msg or '')[:200]}")


def _revoke_token(token: str) -> bool:
    try:
        status, body = _http("POST", "https://oauth2.googleapis.com/revoke",
                          urllib.parse.urlencode({"token": token}).encode(),
                          {"Content-Type": "application/x-www-form-urlencoded"})
    except GoogleError:
        return False
    return status == 200 or (status == 400 and body.get("error") == "invalid_token")  # already invalid


def revoke() -> bool:
    """Tell Google to cancel the saved sign-in (used by setup.sh --uninstall).
    True if it's cancelled (or there was nothing to cancel)."""
    try:
        token = json.loads(TOKEN_FILE.read_text())["refresh_token"]
    except (OSError, ValueError, KeyError):
        return True
    return _revoke_token(token)


if __name__ == "__main__" and len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "--revoke":
    raise SystemExit(0 if revoke() else 1)
