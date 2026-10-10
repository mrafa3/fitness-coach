"""Receive Apple Health data from the Health Auto Export app on your iPhone.

  python receiver.py            # started by launchd (setup.sh); listens on port 8765

The app's REST API automations POST JSON to http://<your-mac>.local:8765/health
with the header "Authorization: Bearer <token>". The token lives in
~/.fitness-coach/receiver_token, created by setup.sh. GET /ping answers "ok" and
touches nothing (setup uses it as a self-test). Two read-only pages, each with its own key:
  GET /training.ics?key=<calendar key>   the plan, for a subscribed calendar (calendar_token)
  GET /report?key=<report key>           the charts report, for your phone's browser (report_token)

Guarded on purpose: only POST /health (and the GETs above), only from private (home network) addresses,
only with the token, capped at 25 MB and 60 seconds per connection, at most 8
connections at once, and every value is range-checked before it's stored
(health.py). After each import the report is rebuilt in a separate process, so it
always uses the current code and can't hang an import.
"""
import hmac
import ipaddress
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import health
from db import HOME, connect

PORT = int(os.environ.get("FC_PORT", "8765"))
MAX_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 60          # whole connection, not per read
MAX_CONNECTIONS = 8
TOKEN_FILE = HOME / "receiver_token"
CALENDAR_TOKEN_FILE = HOME / "calendar_token"   # read-only key for the calendar subscription URL
CALENDAR_FILE = HOME / "training.ics"
REPORT_TOKEN_FILE = HOME / "report_token"       # read-only key for the report page
REPORT_FILE = HOME / "report.html"
# The report is self-contained (charts inlined). This policy stops it from loading or sending
# anything over the network, and no-referrer keeps the key in the URL from leaking.
REPORT_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
EMPTY_CALENDAR = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//fitness-coach//EN\r\nX-WR-CALNAME:Training\r\nEND:VCALENDAR\r\n"
HERE = Path(__file__).parent

_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
_db_lock = threading.Lock()
_report_lock = threading.Lock()


def load_token() -> str:
    try:
        tok = TOKEN_FILE.read_text().strip()
    except OSError:
        tok = ""
    if len(tok) < 32:
        sys.exit(f"{TOKEN_FILE} is missing or too short; run setup.sh again")
    return tok


def _local(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


_last_refusal: dict[str, float] = {}


def log_refusal(ip: str, what: str) -> None:
    """At most one refusal line per address per minute, so junk requests can't fill the disk."""
    import time
    now = time.monotonic()
    if now - _last_refusal.get(ip, -1e9) >= 60:
        if len(_last_refusal) > 1000:
            _last_refusal.clear()
        _last_refusal[ip] = now
        log(f"refused {ip}: {what}")


_rebuild_again = threading.Event()


def rebuild_report() -> None:
    """Run report.py in a fresh process. If one is already running, ask it to run once
    more when it finishes, so data that arrived mid-rebuild still makes it in."""
    if not _report_lock.acquire(blocking=False):
        _rebuild_again.set()
        return
    try:
        while True:
            _rebuild_again.clear()
            try:
                r = subprocess.run([sys.executable, str(HERE / "report.py"), "--quiet"], cwd=HERE,
                                   capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    log(f"report rebuild failed: {r.stderr.strip()[-300:]}")
            except subprocess.TimeoutExpired:
                log("report rebuild timed out")
            if not _rebuild_again.is_set():
                break
    finally:
        _report_lock.release()


class Handler(BaseHTTPRequestHandler):
    timeout = 15              # per read; the watchdog below caps the whole connection
    server_version = "fitness-coach"
    sys_version = ""
    token = ""

    def setup(self):
        super().setup()
        # close the socket if a client is still dribbling bytes after MAX_SECONDS
        self._watchdog = threading.Timer(MAX_SECONDS, self._kill)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _kill(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)  # unblocks a read in progress; close() alone doesn't
        except OSError:
            pass

    def finish(self):
        self._watchdog.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, fmt, *args):  # quieter than the default, and never logs headers
        pass

    def _reply(self, code: int, body: str = "") -> None:
        data = body.encode()
        try:
            self._send_head(code, data)
        except OSError:
            self.close_connection = True
            return

    def _send_head(self, code: int, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:  # client hung up early; nothing to do
            pass
        self.close_connection = True

    def do_GET(self):
        ip = self.client_address[0]
        path, _, query = self.path.partition("?")
        if path == "/ping" and _local(ip):
            return self._reply(200, "ok\n")
        if path == "/training.ics":
            return self._viewer(ip, query, CALENDAR_TOKEN_FILE, CALENDAR_FILE, "text/calendar",
                                EMPTY_CALENDAR.encode(), {}, "calendar")
        if path == "/report":
            return self._viewer(ip, query, REPORT_TOKEN_FILE, REPORT_FILE, "text/html", None,
                                REPORT_HEADERS, "report")
        self._reply(404, "not found\n")

    def _viewer(self, ip: str, query: str, token_file: Path, file: Path, ctype: str,
                fallback: bytes | None, extra: dict, what: str) -> None:
        """A read-only file behind its own key in the URL. Calendar apps and bookmarks can't
        send headers, so each page has a key separate from the upload token."""
        if not _local(ip):
            return self._reply(403, "forbidden\n")
        try:
            expected = token_file.read_text().strip()
        except OSError:
            expected = ""
        key = (parse_qs(query).get("key") or [""])[0]
        if len(expected) < 32 or not hmac.compare_digest(key.encode("utf-8", "replace"), expected.encode()):
            log_refusal(ip, f"{what} request with a bad or missing key")
            return self._reply(404, "not found\n")
        try:
            body = file.read_bytes()
        except OSError:
            if fallback is None:
                return self._reply(404, "No report yet. It appears after the first data arrives.\n")
            body = fallback  # no plan yet: an empty calendar keeps the subscription valid
        try:
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def do_PUT(self):
        self._reply(405, "POST only\n")

    do_DELETE = do_PATCH = do_PUT

    def do_POST(self):
        ip = self.client_address[0]
        if not _local(ip):
            log_refusal(ip, "not on the local network")
            return self._reply(403, "forbidden\n")
        if self.path.split("?")[0] != "/health":
            return self._reply(404, "not found\n")
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode("utf-8", "replace"), f"Bearer {self.token}".encode()):
            log_refusal(ip, "bad or missing token")
            return self._reply(401, "unauthorized\n")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, "Content-Length required\n")
        if not 0 < length <= MAX_BYTES:
            return self._reply(413, "too large\n")
        body = self.rfile.read(length)
        try:
            payload = health.loads(body.decode("utf-8"))
            with _db_lock:
                conn = connect()
                try:
                    counts = health.ingest(conn, payload)
                finally:
                    conn.close()
        except Exception as e:  # any unexpected shape is the sender's problem, not a crash
            log(f"bad payload from {ip}: {type(e).__name__}: {str(e)[:200]}")
            return self._reply(400, "bad payload\n")
        log(f"imported from {ip}: {counts}")
        self._reply(200, "ok\n")
        if counts["weigh_in_values"] or counts["workouts"]:
            rebuild_report()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # one short line, rate-limited per address, instead of a traceback per dropped connection
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # dropped or cut-off connections: routine
            log_refusal(client_address[0], f"connection error ({type(exc).__name__})")
        else:  # a real bug: keep the details, but still rate-limited
            import traceback
            log_refusal(f"bug:{client_address[0]}", "unexpected error:\n" + traceback.format_exc()[-1500:])

    def process_request(self, request, client_address):
        if not _slots.acquire(blocking=False):  # too many open connections: drop this one
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            _slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            _slots.release()


def main() -> None:
    Handler.token = load_token()
    server = Server(("0.0.0.0", PORT), Handler)
    log(f"listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
