"""Refuse to push anything that looks like a secret, your data, or your personal settings.

  python3 check_public.py             # check the whole history, before your first push
  (setup.sh also installs it as a git pre-push hook, so every push is checked;
   `git push --no-verify` skips the hook, so don't use that here)

Three layers:
  1. Allowlist: only code and docs (ALLOWED below, same as .gitignore) may be committed.
  2. Content: no keys, tokens, Health data, databases or paths from your Mac, and none of
     your actual keys from ~/.fitness-coach.
  3. Identity: commits must use your private GitHub noreply email.
On push it checks every commit the remote doesn't have yet, so a secret that was committed
and later deleted is still caught (deleting it in a new commit doesn't remove it from
history). It never prints what it matched, only the file and the reason.
Works with the Python that ships with macOS (3.9+), standard library only.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("FC_HOME", Path.home() / ".fitness-coach"))
MAX_BYTES = 2 * 1024 * 1024
ZERO = "0" * 40

# Keep in step with the "!" lines in .gitignore.
ALLOWED = [".gitignore", "*.py", "README.md", "coach_prompt.md", "setup.sh", "*.example.toml", "schema.sql",
           "LICENSE", "docs/index.html", "docs/privacy.html", "vendor/chart.umd.js",
           "vendor/chartjs-adapter-date-fns.bundle.min.js"]
BAD_NAMES = ["coach.toml", "goals.toml", "run.sh", ".claude_path", "*.db", "*.sqlite*", "*.log",
             "*token*", "google_*.json", "client_secret*.json", "*.ics", "report*.html",
             "export*.json", "*.zip", ".env*", "*.pem", "*.key"]
PATTERNS = [
    (rb"GOCSPX-[A-Za-z0-9_-]{10,}", "a Google client secret"),
    (rb"ya29\.[A-Za-z0-9_-]{20,}", "a Google access token"),
    (rb"1//0[A-Za-z0-9_-]{30,}", "a Google refresh token"),
    (rb'"(refresh_token|client_secret|access_token)"\s*:\s*"[^"]+', "a saved credential"),
    (rb"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (rb"sk-ant-[A-Za-z0-9_-]{10,}", "an Anthropic API key"),
    (rb"(?i)bearer [A-Za-z0-9_-]{32,}", "an upload token"),
    (rb"[?&]key=[A-Za-z0-9_-]{32,}", "a calendar or report key"),
    # Health Auto Export timestamps ("2026-10-05 06:12:00 -0600"), which code never needs
    (rb'"(date|start|end|startDate|endDate)"\s*:\s*"\d{4}-\d\d-\d\d[ T]\d\d:\d\d', "Apple Health data"),
    (rb'<Health[D]ata|<Record type="H[K]', "an Apple Health export"),   # [D], [K]: so this file doesn't match itself
    (rb"<title>Fitness Coac[h]</title>", "your report (it holds your data)"),
    (rb"SQLite format 3\x00", "a database"),
    (rb"/Users/[A-Za-z0-9._-]{2,}", "a path from your Mac (includes your username)"),
]


def git(*args: str, data: bytes = None) -> bytes:
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def own_secrets() -> list:
    """The actual keys on this Mac, so an exact copy anywhere is caught."""
    found = []
    for name in ("receiver_token", "calendar_token", "report_token"):
        try:
            v = (DATA / name).read_text().strip()
        except OSError:
            continue
        if len(v) >= 16:
            found.append(v.encode())
    for name, field in (("google_token.json", "refresh_token"), ("google_client.json", "client_secret")):
        try:
            d = json.loads((DATA / name).read_text())
            v = d.get(field) or (d.get("installed") or d.get("web") or {}).get(field)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(v, str) and len(v) >= 16:
            found.append(v.encode())
    return found


def blobs(revs: list, have: list) -> dict:
    """{blob sha: path} for every file in these commits that the destination doesn't have."""
    if not revs:
        return {}
    out = git("rev-list", "--objects", *revs, *(["--not", *have] if have else []))
    objs = {}
    for line in out.decode("utf-8", "replace").splitlines():
        sha, _, path = line.partition(" ")
        if path:
            objs[sha] = path
    if not objs:
        return {}
    kinds = git("cat-file", "--batch-check=%(objectname) %(objecttype)", data="\n".join(objs).encode() + b"\n")
    keep = {l.split()[0] for l in kinds.decode().splitlines() if l.endswith(" blob")}
    return {s: p for s, p in objs.items() if s in keep}


def allowed(path: str) -> bool:
    """Exact names match the whole path; wildcards (*.py and so on) match the file name."""
    name = path.rsplit("/", 1)[-1]
    return any(path == pat if "*" not in pat else fnmatch.fnmatch(name, pat) for pat in ALLOWED)


def bad_name(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (path.startswith(".venv/") or "__pycache__/" in path or not allowed(path)
            or any(fnmatch.fnmatch(name, pat) for pat in BAD_NAMES))


def new_paths(revs: list, have: list) -> set:
    """Every file path added or changed in commits the destination doesn't have (by name,
    whatever its contents, so a file identical to one already public is still checked)."""
    if not revs:
        return set()
    out = git("-c", "core.quotePath=false", "log", "-z", "--name-only", "--format=", "--no-renames",
              *revs, *(["--not", *have] if have else []))
    return {p.strip("\n") for p in out.decode("utf-8", "replace").split("\0") if p.strip("\n")}


def bad_identity(revs: list, have: list) -> list:
    """Commits whose author or committer email isn't a GitHub noreply address."""
    if not revs:
        return []
    out = git("log", "--format=%h %ae %ce", *revs, *(["--not", *have] if have else []))
    bad = []
    for line in out.decode("utf-8", "replace").splitlines():
        h, *emails = line.split()
        if any(not e.lower().endswith("@users.noreply.github.com") for e in emails):
            bad.append(h)
    return bad


def check(revs: list, have: list) -> list:
    problems = [f"{p}: only code and docs belong in the repo (see ALLOWED in check_public.py)"
                for p in sorted(new_paths(revs, have)) if bad_name(p)]
    ids = bad_identity(revs, have)
    if ids:
        problems.append(f"commit {', '.join(ids[:5])}: your email would be public. Use your GitHub noreply address "
                        "(README: Putting this on GitHub), then: git commit --amend --reset-author --no-edit")
    secrets = own_secrets()
    if revs:  # commit messages are public too
        msgs = git("log", "--format=%h%x00%B%x00", *revs, *(["--not", *have] if have else [])).split(b"\0")
        for h, body in zip(msgs[0::2], msgs[1::2]):
            h = h.strip().decode()
            if any(s in body for s in secrets) or any(re.search(rx, body) for rx, _ in PATTERNS):
                problems.append(f"commit {h}: its message looks like it contains a key, data or a path from your Mac")
    for sha, path in sorted(blobs(revs, have).items(), key=lambda kv: kv[1]):
        if bad_name(path):
            continue  # already reported above
        size = int(git("cat-file", "-s", sha))
        if size > MAX_BYTES and not path.startswith("vendor/"):
            problems.append(f"{path}: unusually large ({size // 1024} KB); exports and databases don't belong here")
            continue
        body = git("cat-file", "blob", sha)
        if any(s in body for s in secrets):
            problems.append(f"{path}: contains one of your actual keys from ~/.fitness-coach")
            continue
        for rx, what in PATTERNS:
            if what.startswith("your report") and path.endswith(".py"):
                continue  # report.py builds the page, so it has the title too
            if re.search(rx, body):
                problems.append(f"{path}: looks like it contains {what}")
                break
    return problems


def known(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode == 0


def main() -> int:
    if "--pre-push" in sys.argv:
        # git gives the remote's name and, per ref, the local and remote commit; exclude only
        # what this remote already has, not what some other remote has
        args = [a for a in sys.argv[1:] if a != "--pre-push"]
        remote = args[0] if args else ""
        revs, have = [], []
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[1] != ZERO:
                revs.append(parts[1])
                if parts[3] != ZERO and known(parts[3]):
                    have.append(parts[3])
        if remote and remote in git("remote").decode().split():
            have.append(f"--remotes={remote}")
    else:
        revs, have = ["HEAD"], []   # by hand: the whole history
        hook = Path(git("rev-parse", "--git-path", "hooks/pre-push").decode().strip())
        if not (hook.exists() and "check_public.py" in hook.read_text(errors="replace")):
            print("Note: the pre-push hook isn't installed, so pushes aren't checked automatically. "
                  "Run: bash setup.sh")
    try:
        problems = check(revs, have)
    except subprocess.CalledProcessError as e:
        print("check_public: couldn't read the repo: " + e.stderr.decode(errors="replace").strip())
        return 1
    if problems:
        print("Not pushing. These files look unsafe to make public:\n  " + "\n  ".join(problems))
        print("\nRemove them from the commits (ask for help if they're already in history), then try again.")
        return 1
    print("check_public: nothing secret or personal found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
