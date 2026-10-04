#!/usr/bin/env python3
"""Live board server for backlog-board. Stdlib only, no dependencies.

Serves a read-only web UI over backlog/epics/*.md + backlog/tasks/*.md + backlog/ADR.md,
with live reload over SSE when those files change on disk. Tasks are still edited by the
agent (or by hand) as markdown files — this server has no write endpoints.

Usage:
    python3 serve.py                  # find project root, daemonize, print URL
    python3 serve.py --root DIR       # explicit project root (dir containing the backlog data)
    python3 serve.py --dir NAME       # backlog data dirname, if not "backlog" (see .backlogrc.json)
    python3 serve.py --foreground     # stay attached, for debugging
    python3 serve.py --context-nudge  # UserPromptSubmit hook: warn on large context
    python3 serve.py --selftest       # parser asserts + live SSE smoke test, no writes
"""
import os
import re
import sys
import json
import time
import fcntl
import signal
import socket
import subprocess
import argparse
import threading
import contextlib
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 3201
CONFIG_FILENAME = "board.config.json"
SESSIONS_FILENAME = ".board-sessions"  # pids of the Claude processes with this project
                                        # open — kept separate from .board, which lives
                                        # and dies with the server process itself
NUDGE_FILENAME = ".board-nudge"        # last context-token count the context-nudge hook
                                        # already fired at, so it re-fires only every
                                        # nudge_every_tokens rather than on every prompt
DEFAULT_ARCHIVE_AFTER_DAYS = 3  # working days, weekends excluded
DEFAULT_REVIEW_AFTER_DAYS = 1   # working days, weekends excluded
DEFAULT_NUDGE_AT_TOKENS = 120_000
DEFAULT_NUDGE_EVERY_TOKENS = 25_000
TRANSCRIPT_TAIL_BYTES = 256 * 1024     # transcripts run to hundreds of MB; the last
                                        # assistant usage line is always near the end
DEFAULT_BACKLOG_DIRNAME = "backlog"
BACKLOG_DIR_MARKER = ".backlogrc.json"  # fixed name, lives at the project root, never
                                         # itself renamed — so it can always be found even
                                         # when the data dir's own name is taken by something
                                         # else in the project
POLL_INTERVAL_SECONDS = 0.7
HEARTBEAT_SECONDS = 15

CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---- frontmatter/task parsing ----
# Mirrors skills/backlog/build.py's parser. Deliberately not imported from there — see
# reference/init.md: build.py is copied into each project so the backlog works without
# the plugin installed, so this file and that one must not depend on each other.

def parse_frontmatter(text):
    """Return (dict of scalar frontmatter fields, body). All values are plain strings —
    this format never nests, so a full YAML parser would be dead weight."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()  # inline comments, e.g. "status: todo # ..."
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, text[m.end():]


def _set_frontmatter_fields(path: Path, updates: dict):
    """Rewrite only the named frontmatter keys in `path`, in place — set an existing
    key's value or append a new one, leave every other line and the whole body
    byte-identical. The only writer of a task/epic file in this codebase (everything
    else here is read-only, see SKILL.md's "the board is read-only"); used exclusively
    by the review auto-accept sweep below to flip `status`/`completed` without a human
    hand-editing the file."""
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return  # not a task/epic file — nothing to do
    lines = m.group(1).splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        key = line.split(":", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}: {remaining.pop(key)}"
    lines.extend(f"{key}: {value}" for key, value in remaining.items())
    new_frontmatter = "---\n" + "\n".join(lines) + "\n---\n"
    path.write_text(new_frontmatter + text[m.end():])


def checkbox_progress(body):
    boxes = CHECKBOX_RE.findall(body)
    done = sum(1 for b in boxes if b.lower() == "x")
    return done, len(boxes)


def load_items(dir_path, kind):
    """rglob so any legacy archive/ subdir is still read until the sweep deletes it."""
    items = []
    if not dir_path.is_dir():
        return items
    for path in sorted(dir_path.rglob("*.md")):
        fields, body = parse_frontmatter(path.read_text())
        if "id" not in fields:
            continue  # not a task/epic file, skip silently
        done, total = checkbox_progress(body)
        fields["_path"] = str(path)
        fields["_body"] = body
        fields["_done"] = done
        fields["_total"] = total
        fields["_kind"] = kind
        items.append(fields)
    return items


def next_id(items, prefix, floor=0):
    nums = [int(m.group(1)) for it in items
            if (m := re.match(rf"{prefix}-(\d+)$", it.get("id", "")))]
    return f"{prefix}-{max(nums + [floor]) + 1:03d}"


def next_id_floor(backlog_dir: Path, prefix: str) -> int:
    """Highest id ever issued, read back from the committed INDEX.md `next:` line, so
    deleting finished items never lets an id be reused."""
    index = backlog_dir / "INDEX.md"
    if not index.is_file():
        return 0
    m = re.search(rf"^next: .*?\b{prefix}-(\d+)", index.read_text(), re.M)
    return int(m.group(1)) - 1 if m else 0


def git_clean_tracked(path: Path) -> bool:
    """True only if path is git-tracked with no uncommitted changes — i.e. deleting it
    loses nothing, since git history still holds the content. Outside a repo: False."""
    def git(*args):
        return subprocess.run(["git", "-C", str(path.parent), *args],
                              capture_output=True, text=True)
    try:
        if git("ls-files", "--error-unmatch", path.name).returncode != 0:
            return False
        status = git("status", "--porcelain", "--", path.name)
    except OSError:
        return False
    return status.returncode == 0 and not status.stdout.strip()


def remove_empty_archive_dirs(backlog_dir: Path):
    for sub in ("tasks", "epics"):
        d = backlog_dir / sub / "archive"
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def derive_epic_status(epic, tasks):
    own = [t for t in tasks if t.get("epic") == epic["id"]]
    if not own:
        return epic.get("status", "todo")
    statuses = {t.get("status", "todo") for t in own}
    if statuses == {"done"}:
        return "done"
    if "doing" in statuses:
        return "doing"
    if "investigate" in statuses:
        return "investigate"
    # every task is either accepted or awaiting acceptance, none still in flight
    if "review" in statuses and statuses <= {"done", "review"}:
        return "review"
    return "todo"


# ---- root detection ----

def resolve_backlog_dirname(root: Path, override=None) -> str:
    """Name of the backlog data directory within `root`. Precedence: explicit override
    (--dir) > BACKLOG_DIR_MARKER's "dir" > "backlog". Lets a project rename the data dir
    when "backlog" itself collides with something else already in the project."""
    if override:
        return override
    marker = root / BACKLOG_DIR_MARKER
    if marker.is_file():
        try:
            name = json.loads(marker.read_text()).get("dir")
            if name:
                return name
        except (OSError, ValueError):
            pass
    return DEFAULT_BACKLOG_DIRNAME


def find_project_root(start: Path, dir_override=None):
    """Walk up from `start` for a directory whose (possibly configured) backlog dirname
    has a tasks/ subdir. Returns (project_root, backlog_dirname), or (None, None)."""
    cur = start.resolve()
    for _ in range(64):
        dirname = resolve_backlog_dirname(cur, dir_override)
        if (cur / dirname / "tasks").is_dir():
            return cur, dirname
        if cur.parent == cur:
            return None, None
        cur = cur.parent
    return None, None


def _read_config(backlog_dir: Path) -> dict:
    """Parse board.config.json, if present. Empty dict on any problem (missing file, bad
    JSON) so callers fall back to their own defaults instead of crashing the launcher."""
    config_path = backlog_dir / CONFIG_FILENAME
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text())
    except (ValueError, OSError):
        return {}


def configured_port(backlog_dir: Path):
    """Read {"port": N} from board.config.json, if present."""
    port = _read_config(backlog_dir).get("port")
    try:
        return int(port) if port is not None else None
    except (ValueError, TypeError):
        return None


def configured_archive_days(backlog_dir: Path):
    """Read {"archive_after_days": N} from board.config.json, if present."""
    days = _read_config(backlog_dir).get("archive_after_days")
    try:
        return int(days) if days is not None else None
    except (ValueError, TypeError):
        return None


def configured_archive_enabled(backlog_dir: Path) -> bool:
    """Read {"archive_enabled": bool} from board.config.json. Defaults to True — the
    automatic startup sweep runs unless explicitly turned off."""
    enabled = _read_config(backlog_dir).get("archive_enabled")
    return True if enabled is None else bool(enabled)


def configured_review_days(backlog_dir: Path):
    """Read {"review_after_days": N} from board.config.json, if present."""
    days = _read_config(backlog_dir).get("review_after_days")
    try:
        return int(days) if days is not None else None
    except (ValueError, TypeError):
        return None


def configured_review_auto_accept(backlog_dir: Path) -> bool:
    """Read {"review_auto_accept": bool} from board.config.json. Defaults to True — a
    `review` task left untouched auto-promotes to `done` after review_after_days unless
    explicitly turned off (a hard human-gate for anyone who wants one)."""
    enabled = _read_config(backlog_dir).get("review_auto_accept")
    return True if enabled is None else bool(enabled)


def configured_nudge_at_tokens(backlog_dir: Path) -> int:
    """Read {"nudge_at_tokens": N} from board.config.json — context size the
    context-nudge hook starts firing at. Falls back to DEFAULT_NUDGE_AT_TOKENS on any
    missing/bad value, same non-crashing shape as the other configured_* accessors."""
    value = _read_config(backlog_dir).get("nudge_at_tokens")
    try:
        return int(value) if value is not None else DEFAULT_NUDGE_AT_TOKENS
    except (ValueError, TypeError):
        return DEFAULT_NUDGE_AT_TOKENS


def configured_nudge_every_tokens(backlog_dir: Path) -> int:
    """Read {"nudge_every_tokens": N} — how much further context has to grow before the
    hook fires again, once past nudge_at_tokens."""
    value = _read_config(backlog_dir).get("nudge_every_tokens")
    try:
        return int(value) if value is not None else DEFAULT_NUDGE_EVERY_TOKENS
    except (ValueError, TypeError):
        return DEFAULT_NUDGE_EVERY_TOKENS


def _parse_date_field(raw):
    """`date.fromisoformat` on a frontmatter value, None on anything missing or
    unparseable — shared by every age-gated field below."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _completed_date(item):
    """Best-effort date an item became done, for retention purposes. Prefers an explicit
    `completed:` field (set by the backlog skill — or the review auto-accept sweep below
    — when status flips to done), falls back to `updated:`, then None if neither parses —
    callers treat None as "not eligible for age-based archiving yet" rather than guessing
    from file mtime, which any unrelated edit would reset."""
    for key in ("completed", "updated"):
        parsed = _parse_date_field(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _business_days_since(start: date, end: date) -> int:
    """Weekday count strictly between start and end, exclusive of start, weekends never
    counted — retention runs in working days, not calendar days."""
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def _epic_completed_date(epic, own_tasks):
    """An epic rarely gets its own `completed:`/`updated:` bump when its last task
    finishes — status is derived, not hand-set. Fall back to the latest completed date
    among its own tasks (the epic is "done" no earlier than its last task finished)."""
    direct = _completed_date(epic)
    if direct is not None:
        return direct
    dates = [d for t in own_tasks if (d := _completed_date(t)) is not None]
    return max(dates) if dates else None


def archive_stale_done(backlog_dir: Path, min_age_days: int):
    """Delete status:done tasks/epics older than min_age_days (working days, weekends
    excluded). Git history is the archive, so only git-tracked, clean files are removed.
    This is the server's automatic startup sweep — age-gated, unlike build.py's manual
    --archive (immediate/unconditional)."""
    epics = load_items(backlog_dir / "epics", "epic")
    tasks = load_items(backlog_dir / "tasks", "task")
    for e in epics:
        e["status"] = derive_epic_status(e, tasks)
    today = date.today()
    removed = []
    for item in tasks + epics:
        if item.get("status") != "done":
            continue
        if item.get("_kind") == "epic":
            own_tasks = [t for t in tasks if t.get("epic") == item.get("id")]
            completed = _epic_completed_date(item, own_tasks)
        else:
            completed = _completed_date(item)
        if completed is None or _business_days_since(completed, today) < min_age_days:
            continue
        src = Path(item["_path"])
        if not git_clean_tracked(src):
            continue
        src.unlink()
        removed.append(src)
    remove_empty_archive_dirs(backlog_dir)
    return removed


def promote_stale_review(backlog_dir: Path, min_age_days: int):
    """Auto-accept: flip status:review tasks older than min_age_days (working days,
    since `updated:` — a human editing the review notes bumps that and buys another
    day) to status:done, stamping `completed:` as the sweep itself, not the skill's
    "set completed: on done" convention this replaces for the auto-accepted path. The
    review lane is the human-supervision gate this whole status exists for — see
    `review_auto_accept` in board.config.json for turning this into a hard gate that
    never auto-promotes. Epics never carry `status: review` directly (derived only),
    so only tasks are ever promoted here."""
    tasks = load_items(backlog_dir / "tasks", "task")
    today = date.today()
    promoted = []
    for t in tasks:
        if t.get("status") != "review":
            continue
        since = _parse_date_field(t.get("updated"))
        if since is None or _business_days_since(since, today) < min_age_days:
            continue
        _set_frontmatter_fields(Path(t["_path"]), {
            "status": "done",
            "completed": today.isoformat(),
        })
        promoted.append(t["id"])
    return promoted


def _run_sweeps(backlog_dir: Path):
    """Startup (and daily, see BoardState._maybe_daily_sweep) age-gated writes: promote
    stale review -> done first, then delete stale done, so a task promoted
    this pass can't also archive in the same pass (its completed: is today)."""
    if configured_review_auto_accept(backlog_dir):
        days = configured_review_days(backlog_dir)
        if days is None:
            days = DEFAULT_REVIEW_AFTER_DAYS
        promoted = promote_stale_review(backlog_dir, days)
        if promoted:
            print(f"backlog board: auto-accepted {len(promoted)} review item(s) "
                  f"({', '.join(promoted)}) after {days}+ day(s)")

    if not configured_archive_enabled(backlog_dir):
        return
    days = configured_archive_days(backlog_dir)
    if days is None:
        days = DEFAULT_ARCHIVE_AFTER_DAYS
    moved = archive_stale_done(backlog_dir, days)
    if moved:
        print(f"backlog board: deleted {len(moved)} item(s) done for {days}+ day(s) (in git history)")


GITIGNORE_CONTENT = (
    "# backlog-board: only markdown content here is version-controlled — everything\n"
    "# else is generated, local runtime state, or copied-in tooling. See SKILL.md.\n"
    "*\n!.gitignore\n!*.md\n!*/\n"
)


def ensure_gitignore(backlog_dir: Path, project_root: Path):
    """Keep backlog/.gitignore enforcing "only .md files committed here" — written/
    overwritten wholesale every run so it self-heals against drift or manual deletion,
    no separate install step. Also appends a .backlogrc.json line to the project's own
    root .gitignore, but only when that marker actually exists and isn't already listed
    — read-check-append only, that file is shared project territory, never rewritten.
    Mirrors build.py's own copy (see this file's module docstring on why the two never
    import each other), except it can reference BACKLOG_DIR_MARKER directly."""
    gi = backlog_dir / ".gitignore"
    if not gi.is_file() or gi.read_text() != GITIGNORE_CONTENT:
        gi.write_text(GITIGNORE_CONTENT)

    marker = project_root / BACKLOG_DIR_MARKER
    if marker.is_file():
        root_gi = project_root / ".gitignore"
        existing = root_gi.read_text() if root_gi.is_file() else ""
        if BACKLOG_DIR_MARKER not in existing:
            sep = "" if not existing or existing.endswith("\n") else "\n"
            with root_gi.open("a") as f:
                f.write(f"{sep}{BACKLOG_DIR_MARKER}\n")


def sync_build_py(backlog_dir: Path):
    """Overwrite <backlog_dir>/build.py with the plugin's own copy, so a plugin update
    reaches every project on its next session start. build.py is gitignored; the plugin
    is its canonical source. No-op if the project has no backlog dir or it's identical."""
    src = Path(__file__).resolve().parent.parent / "skills" / "backlog" / "build.py"
    dest = backlog_dir / "build.py"
    if not backlog_dir.is_dir() or not src.is_file():
        return
    if not dest.is_file() or dest.read_bytes() != src.read_bytes():
        dest.write_bytes(src.read_bytes())


# ---- state: one project's backlog/, watched for changes ----

class BoardState:
    """Owns one project's backlog/ dir: loads it on request, polls it for changes on a
    background thread, and wakes any waiting SSE handlers via a Condition when something
    changed. One instance per server process."""

    def __init__(self, project_root: Path, backlog_dir: Path):
        self.project_root = project_root
        self.backlog_dir = backlog_dir
        self.version = 0
        self.condition = threading.Condition()
        self._snapshot = {}
        # the caller (run_foreground/launch_detached) already ran _run_sweeps once
        # before constructing this — today's sweep is done, only a later day needs one
        self._last_sweep_day = date.today()
        self._poll_once(bump=False)  # establish baseline, don't count it as a "change"

    def _fingerprint(self):
        fp = {}
        for sub in ("epics", "tasks"):
            d = self.backlog_dir / sub
            if not d.is_dir():
                continue
            for path in d.rglob("*.md"):
                st = path.stat()
                fp[str(path)] = (st.st_mtime_ns, st.st_size)
        adr = self.backlog_dir / "ADR.md"
        if adr.is_file():
            st = adr.stat()
            fp[str(adr)] = (st.st_mtime_ns, st.st_size)
        return fp

    def _poll_once(self, bump=True):
        # ponytail: mtime poll, not inotify — fine at backlog sizes we've seen (tens of
        # files); switch to a filesystem-events lib only if a backlog gets big enough
        # for 0.7s polling to be noticeably slow.
        fp = self._fingerprint()
        changed = fp != self._snapshot
        self._snapshot = fp
        if changed and bump:
            with self.condition:
                self.version += 1
                self.condition.notify_all()
        return changed

    def _maybe_daily_sweep(self):
        """Re-run the promote/archive sweeps once per calendar day the server stays up.
        _run_sweeps otherwise only fires at process startup, but the server is reused
        across sessions (see SKILL.md's "The board") — a board left running for a week
        would never promote or archive anything without this. No timer thread: this is
        a cheap date comparison piggybacked on the existing poll loop."""
        today = date.today()
        if today == self._last_sweep_day:
            return
        self._last_sweep_day = today
        _run_sweeps(self.backlog_dir)

    def watch_forever(self, stop_event: threading.Event):
        while not stop_event.is_set():
            self._maybe_daily_sweep()
            self._poll_once()
            stop_event.wait(POLL_INTERVAL_SECONDS)

    def build_state_dict(self):
        epics = load_items(self.backlog_dir / "epics", "epic")
        tasks = load_items(self.backlog_dir / "tasks", "task")
        for e in epics:
            e["status"] = derive_epic_status(e, tasks)
        adr_path = self.backlog_dir / "ADR.md"
        adr_text = adr_path.read_text() if adr_path.is_file() else ""

        def rel(p):
            return os.path.relpath(p, self.backlog_dir)

        review_days = configured_review_days(self.backlog_dir)
        if review_days is None:
            review_days = DEFAULT_REVIEW_AFTER_DAYS

        return {
            "root": str(self.project_root),
            "project": self.project_root.name,
            "version": self.version,
            "review_after_days": review_days,
            "review_auto_accept": configured_review_auto_accept(self.backlog_dir),
            "next": {"task": next_id(tasks, "T", next_id_floor(self.backlog_dir, "T")),
                     "epic": next_id(epics, "E", next_id_floor(self.backlog_dir, "E"))},
            "epics": [
                {"id": e.get("id", ""), "title": e.get("title", ""),
                 "status": e.get("status", "todo"), "created": e.get("created", ""),
                 "updated": e.get("updated", ""), "body": e.get("_body", ""),
                 "path": rel(e["_path"])}
                for e in epics
            ],
            "tasks": [
                {"id": t.get("id", ""), "title": t.get("title", ""),
                 "epic": t.get("epic", ""), "status": t.get("status", "todo"),
                 "priority": t.get("priority", "P3"), "created": t.get("created", ""),
                 "updated": t.get("updated", ""), "docs": t.get("docs", ""),
                 "session_id": t.get("session_id", ""),
                 "done": t["_done"], "total": t["_total"], "body": t.get("_body", ""),
                 "path": rel(t["_path"])}
                for t in tasks
            ],
            "adr": adr_text,
        }


# ---- HTTP handler ----

class BoardRequestHandler(BaseHTTPRequestHandler):
    server_version = "BacklogBoard/0.2"

    def log_message(self, format, *args):  # noqa: A002 — matches BaseHTTPRequestHandler's signature
        pass  # quiet — this is a local dev tool, not worth a log stream

    @property
    def state(self) -> BoardState:
        return self.server.board_state  # type: ignore[attr-defined]

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/state":
            return self._serve_json(self.state.build_state_dict())
        if path == "/api/health":
            return self._serve_json({"ok": True, "root": str(self.state.project_root),
                                      "version": self.state.version})
        if path == "/api/events":
            return self._serve_events()
        self.send_error(404)

    def _serve_static(self, rel_path):
        target = (STATIC_DIR / rel_path).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            return self.send_error(404)
        content_type = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        self._serve_file(target, content_type)

    def _serve_file(self, path: Path, content_type):
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self):
        state = self.state
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_sent = state.version
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                with state.condition:
                    state.condition.wait_for(lambda: state.version != last_sent,
                                              timeout=HEARTBEAT_SECONDS)
                    current = state.version
                if current != last_sent:
                    last_sent = current
                    payload = json.dumps({"version": current}).encode("utf-8")
                    self.wfile.write(b"event: change\ndata: " + payload + b"\n\n")
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ---- server lifecycle ----

def write_board_file(backlog_dir: Path, pid: int, port: int):
    (backlog_dir / ".board").write_text(json.dumps({"pid": pid, "port": port}))


# ---- session registry: which Claude processes still have this project open ----

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    return True


def current_claude_pid():
    """PID of the Claude Code process that spawned us, from the CLAUDE_PID env var it
    sets for hooks and tool calls. None when serve.py is run by hand outside Claude —
    callers then leave the session registry alone entirely."""
    try:
        return int(os.environ.get("CLAUDE_PID", ""))
    except ValueError:
        return None


def _update_sessions(backlog_dir: Path, add=None, remove=None):
    """Read-modify-write <backlog_dir>/.board-sessions, returning the surviving pids.
    Dead pids are pruned on every call, so a session that was kill -9'd (no SessionEnd
    hook) doesn't pin the server forever. flock'd across the whole cycle: two Claude
    instances can start or exit at the same moment, and the lost update that would cause
    is exactly the "killed the board out from under a live session" bug this registry
    exists to prevent."""
    path = backlog_dir / SESSIONS_FILENAME
    with path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            pids = [int(p) for p in json.loads(f.read() or "{}").get("pids", [])]
        except (ValueError, TypeError, AttributeError):
            pids = []  # corrupt/hand-edited file: rebuild from scratch rather than crash
        pids = [p for p in pids if p not in (add, remove) and _pid_alive(p)]
        if add is not None:
            pids.append(add)
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"pids": pids}))
    return pids


def _register_current_session(backlog_dir: Path):
    pid = current_claude_pid()
    if pid is not None:
        _update_sessions(backlog_dir, add=pid)


def _clear_sessions(backlog_dir: Path):
    try:
        (backlog_dir / SESSIONS_FILENAME).unlink()
    except OSError:
        pass


_hook_stdin_cache = None  # sentinel: None = not yet read this process, {} = read and empty/absent


def _hook_stdin_payload():
    """The hook payload Claude Code writes to stdin, parsed once and cached — stdin is a
    stream, a second json.load() against it after the first would see EOF and silently
    return nothing. {} for a hand-run invocation (tty, or anything that isn't a JSON
    object) so callers can .get() it unconditionally. One real process only ever handles
    one hook invocation, so process-lifetime caching is safe there; selftest simulates
    several invocations in one process and resets this via _stdin_json below."""
    global _hook_stdin_cache
    if _hook_stdin_cache is None:
        payload = {}
        if not (sys.stdin is None or sys.stdin.isatty()):
            try:
                payload = json.load(sys.stdin)
            except (ValueError, OSError, AttributeError):
                payload = {}
        _hook_stdin_cache = payload if isinstance(payload, dict) else {}
    return _hook_stdin_cache


def _hook_stdin_field(name):
    """Value of `name` from the cached hook payload (see _hook_stdin_payload), or None."""
    return _hook_stdin_payload().get(name)


# ---- context-nudge: UserPromptSubmit hook warning when context is getting large ----

def _transcript_context_tokens(transcript_path) -> int:
    """Exact current context size, read from the transcript JSONL Claude Code already
    writes — not an estimate. Each assistant line's message.usage carries the token
    counts the model was actually charged for; the last one in the file is the current
    total (input + everything cached in either direction). Only the tail is read since
    a long-running session's transcript can reach hundreds of MB."""
    if not transcript_path:
        return 0
    path = Path(transcript_path)
    if not path.is_file():
        return 0
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return 0
    for line in reversed(tail.split(b"\n")):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # first line after the seek cut may be a truncated fragment
        usage = (obj.get("message") or {}).get("usage")
        if obj.get("type") == "assistant" and isinstance(usage, dict):
            return (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
    return 0


def _last_nudged_tokens(backlog_dir: Path) -> int:
    path = backlog_dir / NUDGE_FILENAME
    if not path.is_file():
        return 0
    try:
        return int(json.loads(path.read_text()).get("tokens", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _write_nudged_tokens(backlog_dir: Path, tokens: int):
    (backlog_dir / NUDGE_FILENAME).write_text(json.dumps({"tokens": tokens}))


def context_nudge(backlog_dir: Path):
    """UserPromptSubmit hook path: warn once context is large *and* this session has a
    task actually in flight — silent otherwise (no stdout at all), since this hook runs
    on every prompt in every project, most of which have no backlog dir or nothing doing
    right now. When it does fire, the message goes out as hookSpecificOutput JSON — the
    format Claude Code's UserPromptSubmit hooks use to inject additionalContext; this is
    unconditional on --quiet, which only gates the earlier "no backlog dir" error path."""
    session_id = _hook_stdin_field("session_id")
    transcript_path = _hook_stdin_field("transcript_path")
    if not session_id or not transcript_path:
        return

    tokens = _transcript_context_tokens(transcript_path)
    threshold = configured_nudge_at_tokens(backlog_dir)
    if tokens < threshold:
        return

    tasks = load_items(backlog_dir / "tasks", "task")
    # status == "doing" only — an investigate task (open investigation: a decision,
    # a code dig, a websearch, see SKILL.md's "When a task or epic isn't ready") never
    # matches here, so investigating one never trips this threshold, by construction.
    doing = next((t for t in tasks if t.get("status") == "doing"
                  and t.get("session_id") == session_id), None)
    if doing is None:
        return

    every = configured_nudge_every_tokens(backlog_dir)
    if tokens - _last_nudged_tokens(backlog_dir) < every:
        return
    _write_nudged_tokens(backlog_dir, tokens)

    message = (f"Context ~{tokens // 1000}k. {doing.get('id', '?')} in flight — at next "
               f"stopping point, write the follow-up task and offer the session boundary.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": message}}))


def probe_health(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_free(port: int) -> bool:
    """True if nothing is currently listening on 127.0.0.1:port. A fixed, predictable
    port is the whole point of backlog/board.config.json — silently picking a different
    one when it's taken would defeat that, so callers check this first and warn instead."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _port_busy_message(port: int, backlog_dir: Path) -> str:
    return (f"backlog board: port {port} is already in use by something else (not this "
            f"project's board) — not starting. Free the port, or set a different one in "
            f"{backlog_dir.name}/{CONFIG_FILENAME} (\"port\": N) or pass --port.")


def make_server(project_root: Path, backlog_dir: Path, port: int):
    """Build the HTTPServer + its BoardState. Callers are expected to have already
    confirmed the port is free (_port_free) — this doesn't fall back to a different one."""
    state = BoardState(project_root, backlog_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), BoardRequestHandler)
    httpd.board_state = state  # type: ignore[attr-defined]
    return httpd, state


def serve_with_lifecycle(httpd: ThreadingHTTPServer, state: BoardState):
    stop_event = threading.Event()
    threading.Thread(target=state.watch_forever, args=(stop_event,), daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        _cleanup(state)


def _cleanup(state: BoardState):
    board_file = state.backlog_dir / ".board"
    try:
        if board_file.is_file() and json.loads(board_file.read_text()).get("pid") == os.getpid():
            board_file.unlink()
    except OSError:
        pass


def stop_server(backlog_dir: Path, quiet: bool = False):
    """Kill the running board server for this project (SIGTERM the pid recorded in
    <backlog_dir>/.board) and remove that file. Doesn't wait for the process to actually
    exit — the file is the source of truth for "is a server registered here", and we
    own removing it, so there's nothing to race.

    Unconditional by design: an explicit "stop it now" overrides the session registry,
    which is also cleared so the next --session-end isn't a no-op against stale pids."""
    _clear_sessions(backlog_dir)
    board_file = backlog_dir / ".board"
    if not board_file.is_file():
        if not quiet:
            print(f"backlog board: not running (no {backlog_dir.name}/.board)", file=sys.stderr)
        return
    pid = None
    try:
        pid = json.loads(board_file.read_text()).get("pid")
    except (OSError, ValueError):
        pass
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already dead
    try:
        board_file.unlink()
    except OSError:
        pass
    if not quiet:
        print(f"backlog board: stopped (pid {pid})" if pid else "backlog board: stopped")


def session_end(backlog_dir: Path, quiet: bool = False):
    """SessionEnd hook path: drop this Claude process from the session registry, and
    stop the server only if it was the last one holding this project open. Another
    session (another terminal tab on the same project) still registered means the board
    stays up — closing one window must not kill the board out from under the others.

    /clear also fires SessionEnd, immediately followed by a SessionStart that would
    start the server right back up; skip that case entirely rather than flap."""
    if _hook_stdin_field("reason") == "clear":
        return
    remaining = _update_sessions(backlog_dir, remove=current_claude_pid())
    if remaining:
        if not quiet:
            print(f"backlog board: left running — {len(remaining)} other session(s) "
                  f"still have this project open")
        return
    stop_server(backlog_dir, quiet=quiet)


def run_foreground(project_root: Path, backlog_dir: Path, port: int):
    sync_build_py(backlog_dir)
    existing = probe_health(port)
    if existing and existing.get("root") == str(project_root):
        _register_current_session(backlog_dir)
        print(f"http://127.0.0.1:{port}/  (reusing running server)")
        return
    if not _port_free(port):
        print(_port_busy_message(port, backlog_dir))
        return
    _run_sweeps(backlog_dir)
    ensure_gitignore(backlog_dir, project_root)
    _register_current_session(backlog_dir)
    httpd, state = make_server(project_root, backlog_dir, port)
    chosen_port = httpd.server_address[1]
    write_board_file(state.backlog_dir, os.getpid(), chosen_port)
    print(f"http://127.0.0.1:{chosen_port}/")
    serve_with_lifecycle(httpd, state)


def launch_detached(project_root: Path, backlog_dir: Path, port: int):
    """Fork once: parent prints the URL and returns immediately (so a slash command
    invoking this script completes), child setsid()s to detach from the controlling
    terminal and keeps serving after the parent's shell exits."""
    sync_build_py(backlog_dir)
    existing = probe_health(port)
    if existing and existing.get("root") == str(project_root):
        _register_current_session(backlog_dir)
        print(f"http://127.0.0.1:{port}/  (reusing running server)")
        return
    if not _port_free(port):
        print(_port_busy_message(port, backlog_dir))
        return
    _run_sweeps(backlog_dir)
    ensure_gitignore(backlog_dir, project_root)
    _register_current_session(backlog_dir)

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(write_fd)
        with os.fdopen(read_fd) as r:
            line = r.readline().strip()
        if not line:
            print("backlog board: server failed to start", file=sys.stderr)
            sys.exit(1)
        chosen_port = int(line)
        write_board_file(backlog_dir, pid, chosen_port)
        print(f"http://127.0.0.1:{chosen_port}/")
        return

    # child: detach, then serve until killed (SessionEnd hook or manual --stop)
    os.close(read_fd)
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    httpd, state = make_server(project_root, backlog_dir, port)
    chosen_port = httpd.server_address[1]
    with os.fdopen(write_fd, "w") as w:
        w.write(f"{chosen_port}\n")
    serve_with_lifecycle(httpd, state)
    os._exit(0)


# ---- selftest ----

def _selftest_parsers():
    fm, body = parse_frontmatter(
        "---\nid: T-001\nstatus: todo\n---\n## Subtasks\n- [x] a\n- [ ] b\n"
    )
    assert fm == {"id": "T-001", "status": "todo"}, fm
    assert checkbox_progress(body) == (1, 2)

    items = [{"id": "T-001"}, {"id": "T-003"}]
    assert next_id(items, "T") == "T-004"
    assert next_id([], "E") == "E-001"

    epic = {"id": "E-001"}
    assert derive_epic_status(
        epic, [{"epic": "E-001", "status": "doing"}, {"epic": "E-001", "status": "todo"}]
    ) == "doing"
    assert derive_epic_status(epic, [{"epic": "E-001", "status": "done"}]) == "done"
    assert derive_epic_status(epic, []) == "todo"
    # investigate: outranks todo, but doing still wins over it
    assert derive_epic_status(
        epic, [{"epic": "E-001", "status": "investigate"}, {"epic": "E-001", "status": "todo"}]
    ) == "investigate"
    assert derive_epic_status(
        epic, [{"epic": "E-001", "status": "doing"}, {"epic": "E-001", "status": "investigate"}]
    ) == "doing"
    # task-less epic can carry investigate directly on its own frontmatter
    assert derive_epic_status({"id": "E-001", "status": "investigate"}, []) == "investigate"
    # review: every task accepted or awaiting acceptance, none still in flight
    assert derive_epic_status(
        epic, [{"epic": "E-001", "status": "review"}, {"epic": "E-001", "status": "done"}]
    ) == "review"
    assert derive_epic_status(epic, [{"epic": "E-001", "status": "review"}]) == "review"
    # doing still outranks review — an epic with one task still in flight isn't "review"
    assert derive_epic_status(
        epic, [{"epic": "E-001", "status": "review"}, {"epic": "E-001", "status": "doing"}]
    ) == "doing"


def _selftest_live():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "backlog" / "tasks").mkdir(parents=True)
        (root / "backlog" / "epics").mkdir(parents=True)
        (root / "backlog" / "tasks" / "T-001-x.md").write_text(
            "---\nid: T-001\nepic: E-001\nstatus: todo\ntitle: First\n---\n- [ ] a\n"
        )
        (root / "backlog" / "epics" / "E-001-x.md").write_text(
            "---\nid: E-001\ntitle: Only epic\n---\n"
        )

        httpd, state = make_server(root, root / "backlog", 0)
        port = httpd.server_address[1]
        stop_event = threading.Event()
        threading.Thread(target=state.watch_forever, args=(stop_event,), daemon=True).start()
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1},
                          daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as r:
                payload = json.loads(r.read().decode("utf-8"))
            assert len(payload["tasks"]) == 1, payload["tasks"]
            assert payload["tasks"][0]["id"] == "T-001"
            assert payload["next"]["task"] == "T-002"
            assert payload["next"]["epic"] == "E-002"

            def write_new_task():
                time.sleep(0.3)
                (root / "backlog" / "tasks" / "T-002-y.md").write_text(
                    "---\nid: T-002\nepic: E-001\nstatus: todo\ntitle: Second\n---\n"
                )

            threading.Thread(target=write_new_task, daemon=True).start()

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events", timeout=5) as r:
                deadline = time.time() + 4
                saw_change = False
                while time.time() < deadline:
                    line = r.readline()
                    if not line:
                        break
                    if line.startswith(b"event: change"):
                        saw_change = True
                        break
                assert saw_change, "no SSE change event within timeout"
        finally:
            httpd.shutdown()
            httpd.server_close()


def _selftest_custom_dirname():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / BACKLOG_DIR_MARKER).write_text(json.dumps({"dir": "dev-tracker"}))
        (root / "dev-tracker" / "tasks").mkdir(parents=True)
        (root / "dev-tracker" / "epics").mkdir(parents=True)

        found_root, found_dirname = find_project_root(root)
        assert found_root == root.resolve(), found_root
        assert found_dirname == "dev-tracker", found_dirname

        # --dir override wins even over the marker file (checked directly — an override
        # naming a dir that doesn't exist correctly fails the walk, tested separately below)
        assert resolve_backlog_dirname(root, override="something-else") == "something-else"

        # walking up from a nested cwd still finds it
        nested = root / "some" / "nested" / "cwd"
        nested.mkdir(parents=True)
        found_root2, found_dirname2 = find_project_root(nested)
        assert found_root2 == root.resolve(), found_root2
        assert found_dirname2 == "dev-tracker", found_dirname2

        # an override naming a dir that doesn't actually exist correctly fails the walk
        assert find_project_root(root, dir_override="nonexistent-name") == (None, None)

        # no marker, no override -> plain "backlog" default, and none exists here
        with tempfile.TemporaryDirectory() as tmp2:
            assert find_project_root(Path(tmp2)) == (None, None)


def _selftest_business_days_since():
    # Mon Aug 10 2026 -> Fri Aug 14: 4 business days, no weekend crossed.
    assert _business_days_since(date(2026, 8, 10), date(2026, 8, 14)) == 4
    # Fri Aug 14 -> Mon Aug 17: 1 business day, weekend excluded from the count.
    assert _business_days_since(date(2026, 8, 14), date(2026, 8, 17)) == 1
    # Fri Aug 14 -> Sun Aug 16: 0 business days, entirely weekend.
    assert _business_days_since(date(2026, 8, 14), date(2026, 8, 16)) == 0
    # same day -> 0
    assert _business_days_since(date(2026, 8, 14), date(2026, 8, 14)) == 0


def _git_commit_all(repo: Path):
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)


def _selftest_archive_retention():
    import tempfile
    from datetime import timedelta
    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp) / "backlog"
        (backlog_dir / "tasks").mkdir(parents=True)
        (backlog_dir / "epics").mkdir(parents=True)
        old_date = (date.today() - timedelta(days=10)).isoformat()
        recent_date = (date.today() - timedelta(days=1)).isoformat()

        (backlog_dir / "tasks" / "T-001-old.md").write_text(
            f"---\nid: T-001\nepic: E-001\nstatus: done\ncompleted: {old_date}\n---\n"
        )
        (backlog_dir / "tasks" / "T-002-recent.md").write_text(
            f"---\nid: T-002\nepic: E-001\nstatus: done\ncompleted: {recent_date}\n---\n"
        )
        (backlog_dir / "tasks" / "T-003-nodate.md").write_text(
            "---\nid: T-003\nepic: E-001\nstatus: done\n---\n"
        )
        (backlog_dir / "epics" / "E-001-x.md").write_text(
            "---\nid: E-001\ntitle: Test\n---\n"
        )
        (backlog_dir / "INDEX.md").write_text("next: T-004 · E-002\n")

        # outside a git repo nothing is deleted (no history to recover from)
        assert archive_stale_done(backlog_dir, min_age_days=3) == []
        assert (backlog_dir / "tasks" / "T-001-old.md").exists()

        _git_commit_all(Path(tmp))
        removed = {p.name for p in archive_stale_done(backlog_dir, min_age_days=3)}
        assert removed == {"T-001-old.md"}, removed
        assert not (backlog_dir / "tasks" / "T-001-old.md").exists()
        assert (backlog_dir / "tasks" / "T-002-recent.md").exists()  # too recent
        assert (backlog_dir / "tasks" / "T-003-nodate.md").exists()  # no date, left alone
        assert (backlog_dir / "epics" / "E-001-x.md").exists()  # newest child is recent
        # uncommitted edit -> not deleted
        recent = backlog_dir / "tasks" / "T-002-recent.md"
        recent.write_text(f"---\nid: T-002\nepic: E-001\nstatus: done\ncompleted: {old_date}\n---\n")
        # (the epic is clean and now old enough, so only it goes)
        assert {p.name for p in archive_stale_done(backlog_dir, min_age_days=3)} == {"E-001-x.md"}
        assert recent.exists()

        _git_commit_all(Path(tmp))
        removed2 = {p.name for p in archive_stale_done(backlog_dir, min_age_days=3)}
        assert removed2 == {"T-002-recent.md"}, removed2
        assert archive_stale_done(backlog_dir, min_age_days=3) == []

        # deleting T-001/T-002 must not let ids be reused
        remaining = load_items(backlog_dir / "tasks", "task")
        assert next_id(remaining, "T", next_id_floor(backlog_dir, "T")) == "T-004"


def _selftest_archive_toggle():
    import tempfile
    from datetime import timedelta
    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp) / "backlog"
        (backlog_dir / "tasks").mkdir(parents=True)
        (backlog_dir / "epics").mkdir(parents=True)
        old_date = (date.today() - timedelta(days=10)).isoformat()
        (backlog_dir / "tasks" / "T-001-old.md").write_text(
            f"---\nid: T-001\nepic: E-001\nstatus: done\ncompleted: {old_date}\n---\n"
        )

        _git_commit_all(Path(tmp))
        assert configured_archive_enabled(backlog_dir) is True  # no config file -> default on

        (backlog_dir / CONFIG_FILENAME).write_text(json.dumps({"archive_enabled": False}))
        assert configured_archive_enabled(backlog_dir) is False
        _run_sweeps(backlog_dir)
        assert (backlog_dir / "tasks" / "T-001-old.md").exists()  # sweep skipped, nothing moved

        (backlog_dir / CONFIG_FILENAME).write_text(
            json.dumps({"archive_enabled": True, "archive_after_days": 3})
        )
        assert configured_archive_enabled(backlog_dir) is True
        _run_sweeps(backlog_dir)
        assert not (backlog_dir / "tasks" / "T-001-old.md").exists()  # now swept


def _selftest_frontmatter_write():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "T-001-x.md"
        original = (
            "---\nid: T-001\nepic: E-001\nstatus: review\nupdated: 2026-08-10\n"
            "title: Something # inline comment preserved\n---\n"
            "## Context\nBody text, untouched.\n- [x] a\n- [ ] b\n"
        )
        path.write_text(original)

        _set_frontmatter_fields(path, {"status": "done", "completed": "2026-08-17"})
        fields, body = parse_frontmatter(path.read_text())
        assert fields["status"] == "done", fields
        assert fields["completed"] == "2026-08-17", fields
        # untouched keys survive exactly, including the one carrying an inline comment
        assert fields["id"] == "T-001" and fields["epic"] == "E-001", fields
        assert fields["updated"] == "2026-08-10", fields
        assert body == "## Context\nBody text, untouched.\n- [x] a\n- [ ] b\n", repr(body)

        # appending a key that wasn't present at all
        _set_frontmatter_fields(path, {"session_id": "sess-a"})
        fields2, _ = parse_frontmatter(path.read_text())
        assert fields2["session_id"] == "sess-a", fields2
        assert fields2["status"] == "done", fields2  # earlier write still holds


def _selftest_review_promotion():
    import tempfile
    from datetime import timedelta
    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp) / "backlog"
        (backlog_dir / "tasks").mkdir(parents=True)
        (backlog_dir / "epics").mkdir(parents=True)
        stale_date = (date.today() - timedelta(days=10)).isoformat()  # well past 1 working day
        fresh_date = date.today().isoformat()

        (backlog_dir / "tasks" / "T-001-stale.md").write_text(
            f"---\nid: T-001\nepic: E-001\nstatus: review\nupdated: {stale_date}\n---\n"
        )
        (backlog_dir / "tasks" / "T-002-fresh.md").write_text(
            f"---\nid: T-002\nepic: E-001\nstatus: review\nupdated: {fresh_date}\n---\n"
        )

        promoted = promote_stale_review(backlog_dir, min_age_days=1)
        assert promoted == ["T-001"], promoted

        fields1, _ = parse_frontmatter((backlog_dir / "tasks" / "T-001-stale.md").read_text())
        assert fields1["status"] == "done", fields1
        assert fields1["completed"] == date.today().isoformat(), fields1

        fields2, _ = parse_frontmatter((backlog_dir / "tasks" / "T-002-fresh.md").read_text())
        assert fields2["status"] == "review", fields2  # too recent, untouched

        # promoted-this-pass never archives in the same pass: completed: is today,
        # which is under any positive archive_after_days
        _run_sweeps(backlog_dir)  # review_auto_accept defaults on, archive defaults on
        assert (backlog_dir / "tasks" / "T-001-stale.md").exists()
        assert not (backlog_dir / "tasks" / "archive" / "T-001-stale.md").exists()

        # re-running is a no-op — already done, not stale enough to archive yet
        assert promote_stale_review(backlog_dir, min_age_days=1) == []


def _selftest_review_auto_accept_toggle():
    import tempfile
    from datetime import timedelta
    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp) / "backlog"
        (backlog_dir / "tasks").mkdir(parents=True)
        (backlog_dir / "epics").mkdir(parents=True)
        stale_date = (date.today() - timedelta(days=10)).isoformat()
        (backlog_dir / "tasks" / "T-001-stale.md").write_text(
            f"---\nid: T-001\nepic: E-001\nstatus: review\nupdated: {stale_date}\n---\n"
        )

        (backlog_dir / CONFIG_FILENAME).write_text(json.dumps({"review_auto_accept": False}))
        assert configured_review_auto_accept(backlog_dir) is False
        _run_sweeps(backlog_dir)
        fields, _ = parse_frontmatter((backlog_dir / "tasks" / "T-001-stale.md").read_text())
        assert fields["status"] == "review", fields  # gate held, no auto-promotion

        (backlog_dir / CONFIG_FILENAME).write_text(
            json.dumps({"review_auto_accept": True, "review_after_days": 1})
        )
        _run_sweeps(backlog_dir)
        fields2, _ = parse_frontmatter((backlog_dir / "tasks" / "T-001-stale.md").read_text())
        assert fields2["status"] == "done", fields2


def _selftest_sync_build_py():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp) / "backlog"
        sync_build_py(backlog_dir)  # no backlog dir -> no-op, no crash
        assert not backlog_dir.exists()
        backlog_dir.mkdir()
        (backlog_dir / "build.py").write_text("stale")
        sync_build_py(backlog_dir)
        plugin_copy = Path(__file__).resolve().parent.parent / "skills" / "backlog" / "build.py"
        assert (backlog_dir / "build.py").read_bytes() == plugin_copy.read_bytes()


def _selftest_ensure_gitignore():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        backlog_dir = project_root / "backlog"
        backlog_dir.mkdir()

        ensure_gitignore(backlog_dir, project_root)
        gi_path = backlog_dir / ".gitignore"
        assert gi_path.read_text() == GITIGNORE_CONTENT

        mtime_before = gi_path.stat().st_mtime_ns
        ensure_gitignore(backlog_dir, project_root)
        assert gi_path.stat().st_mtime_ns == mtime_before  # unchanged content -> no rewrite

        # .backlogrc.json at the project root -> exactly one line appended to root
        # .gitignore, existing unrelated content left untouched, repeats don't duplicate
        (project_root / ".gitignore").write_text("some_other_tool_output/\n")
        (project_root / BACKLOG_DIR_MARKER).write_text('{"dir": "backlog"}')
        ensure_gitignore(backlog_dir, project_root)
        root_gi_text = (project_root / ".gitignore").read_text()
        assert "some_other_tool_output/" in root_gi_text
        assert root_gi_text.count(BACKLOG_DIR_MARKER) == 1, root_gi_text
        ensure_gitignore(backlog_dir, project_root)
        assert (project_root / ".gitignore").read_text().count(BACKLOG_DIR_MARKER) == 1


def _selftest_sessions():
    import tempfile
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp)
        finished = subprocess.Popen(["true"])
        finished.wait()  # reaped -> its pid is definitely not alive anymore
        live_a, live_b = os.getpid(), os.getppid()

        assert _update_sessions(backlog_dir, add=live_a) == [live_a]
        assert _update_sessions(backlog_dir, add=live_a) == [live_a]  # no duplicate
        assert _update_sessions(backlog_dir, add=live_b) == [live_a, live_b]

        # a session that died without running its SessionEnd hook is pruned
        _update_sessions(backlog_dir, add=finished.pid)
        assert _update_sessions(backlog_dir) == [live_a, live_b]

        # one of two sessions exiting -> the other still holds the project open
        assert _update_sessions(backlog_dir, remove=live_b) == [live_a]
        # last one out -> empty -> session_end() stops the server
        assert _update_sessions(backlog_dir, remove=live_a) == []

        # corrupt registry rebuilds instead of crashing the hook
        (backlog_dir / SESSIONS_FILENAME).write_text("not json{")
        assert _update_sessions(backlog_dir, add=live_a) == [live_a]

        # run by hand, outside Claude: nothing registered at all
        saved = os.environ.pop("CLAUDE_PID", None)
        try:
            assert current_claude_pid() is None
            _register_current_session(backlog_dir)
            assert _update_sessions(backlog_dir) == [live_a]
        finally:
            if saved is not None:
                os.environ["CLAUDE_PID"] = saved

        _clear_sessions(backlog_dir)
        assert not (backlog_dir / SESSIONS_FILENAME).exists()
        _clear_sessions(backlog_dir)  # already gone -> no-op, not an error


def _selftest_context_nudge():
    import io
    import tempfile

    def transcript(tmp, tokens, session_id="sess-a"):
        """One assistant line carrying the given total context-token count."""
        path = Path(tmp) / "transcript.jsonl"
        line = {
            "type": "assistant", "sessionId": session_id,
            "message": {"usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": tokens}},
        }
        path.write_text(json.dumps(line) + "\n")
        return path

    def run(backlog_dir, stdin_obj):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with _stdin_json(stdin_obj):
                context_nudge(backlog_dir)
        return buf.getvalue()

    with tempfile.TemporaryDirectory() as tmp:
        backlog_dir = Path(tmp) / "backlog"
        (backlog_dir / "tasks").mkdir(parents=True)
        assert _transcript_context_tokens(str(transcript(tmp, 150_000))) == 150_000
        assert _transcript_context_tokens("/no/such/file") == 0
        assert _transcript_context_tokens(None) == 0

        (backlog_dir / "tasks" / "T-001-x.md").write_text(
            "---\nid: T-001\nepic: E-001\nstatus: doing\nsession_id: sess-a\n---\n"
        )

        # below threshold -> silent
        low = transcript(tmp, 50_000)
        assert run(backlog_dir, {"session_id": "sess-a", "transcript_path": str(low)}) == ""

        # over threshold, matching doing task -> fires, names the task
        high = transcript(tmp, 150_000)
        out = run(backlog_dir, {"session_id": "sess-a", "transcript_path": str(high)})
        assert "T-001" in out and "additionalContext" in out, out

        # immediately re-firing below nudge_every_tokens -> silent
        assert run(backlog_dir, {"session_id": "sess-a", "transcript_path": str(high)}) == ""

        # task belongs to a different session -> silent even over threshold
        other = transcript(tmp, 150_000, session_id="sess-b")
        assert run(backlog_dir, {"session_id": "sess-b", "transcript_path": str(other)}) == ""

        # investigate task (open investigation) on this session, no doing task at all ->
        # silent even over threshold — the token limit never applies to an investigation
        (backlog_dir / "tasks" / "T-002-y.md").write_text(
            "---\nid: T-002\nepic: E-001\nstatus: investigate\nsession_id: sess-c\n---\n"
        )
        investigating = transcript(tmp, 150_000, session_id="sess-c")
        assert run(backlog_dir, {"session_id": "sess-c", "transcript_path": str(investigating)}) == ""

        # missing hook stdin fields -> silent, no crash
        assert run(backlog_dir, {}) == ""


@contextlib.contextmanager
def _stdin_json(obj):
    """Feed a JSON payload to _hook_stdin_field for the duration of the block, by
    swapping sys.stdin the same way Claude Code's hook invocation does (a pipe, not a
    tty). Also resets the module-level stdin cache on both sides — selftest calls this
    multiple times in one process, standing in for what's normally one process per
    real hook invocation."""
    import io
    global _hook_stdin_cache
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(obj))
    _hook_stdin_cache = None
    try:
        yield
    finally:
        sys.stdin = old_stdin
        _hook_stdin_cache = None


def selftest():
    _selftest_parsers()
    _selftest_custom_dirname()
    _selftest_business_days_since()
    _selftest_archive_retention()
    _selftest_archive_toggle()
    _selftest_frontmatter_write()
    _selftest_review_promotion()
    _selftest_review_auto_accept_toggle()
    _selftest_ensure_gitignore()
    _selftest_sync_build_py()
    _selftest_sessions()
    _selftest_context_nudge()
    _selftest_live()
    print("selftest OK")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live board server for backlog-board.")
    parser.add_argument("--root", type=Path, default=None,
                         help="project root (dir containing the backlog data dir); "
                              "auto-detected from cwd if omitted")
    parser.add_argument("--dir", type=str, default=None,
                         help=f"backlog data dirname within the project root (default: "
                              f"\"{DEFAULT_BACKLOG_DIRNAME}\", or {BACKLOG_DIR_MARKER}'s "
                              f"\"dir\" if set) — use when that name collides with "
                              f"something else already in the project")
    parser.add_argument("--port", type=int, default=None,
                         help=f"defaults to the backlog dir's {CONFIG_FILENAME} \"port\" "
                              f"if set, else {DEFAULT_PORT}")
    parser.add_argument("--foreground", action="store_true",
                         help="stay attached instead of daemonizing (for debugging)")
    parser.add_argument("--stop", action="store_true",
                         help="stop the running board server for this project and exit, "
                              "unconditionally (reads/removes <backlog_dir>/.board) — the "
                              "manual \"shut it down now\"; see --session-end for the hook")
    parser.add_argument("--session-end", action="store_true",
                         help="SessionEnd hook path: deregister this Claude process and stop "
                              "the server only if no other session still has this project open")
    parser.add_argument("--context-nudge", action="store_true",
                         help="UserPromptSubmit hook path: warn once context is large and a "
                              "doing task belongs to this session — emits hookSpecificOutput "
                              "JSON when it fires, nothing otherwise")
    parser.add_argument("--quiet", action="store_true",
                         help="exit 0 with no output for the \"no backlog dir found\" case, "
                              "and (with --stop/--session-end) the \"not running\" case — for a hook "
                              "that runs unconditionally across every project, most of which have "
                              "no backlog dir")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.root:
        root = args.root.resolve()
        dirname = resolve_backlog_dirname(root, args.dir)
    else:
        root, dirname = find_project_root(Path.cwd(), args.dir)

    if root is None:
        if args.quiet:
            return
        probe_name = args.dir or DEFAULT_BACKLOG_DIRNAME
        print(f"backlog board: no {probe_name}/tasks/ found in this directory or any parent. "
              "Run backlog init first (see reference/init.md).", file=sys.stderr)
        sys.exit(1)
    assert dirname is not None  # implied by root being resolved, see find_project_root

    backlog_dir = root / dirname

    if args.context_nudge:
        return context_nudge(backlog_dir)

    if args.session_end:
        return session_end(backlog_dir, quiet=args.quiet)

    if args.stop:
        return stop_server(backlog_dir, quiet=args.quiet)

    port: int = args.port if args.port is not None else (configured_port(backlog_dir) or DEFAULT_PORT)

    if args.foreground:
        run_foreground(root, backlog_dir, port)
    else:
        launch_detached(root, backlog_dir, port)


if __name__ == "__main__":
    main()
