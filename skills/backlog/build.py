#!/usr/bin/env python3
"""Generate backlog/INDEX.md from epics/*.md + tasks/*.md.

Stdlib only. Run from anywhere inside the project; it locates its own directory.
Usage:
    python3 backlog/build.py             # rebuild INDEX.md
    python3 backlog/build.py --archive   # delete done tasks/epics (git-tracked, clean only), then rebuild
    python3 backlog/build.py --selftest  # run inline asserts, no filesystem writes

For a live, searchable board, install the backlog-board plugin and run
`/backlog-board` — see the plugin's server/ (not part of this file, which stays
dependency-free so it works standalone in a project without the plugin installed).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
DEFERRAL_REF_RE = re.compile(r"T-\d{3}")
SUBTASK_LINE_RE = re.compile(r"^(\s*)-\s*\[([ xX])\]")
HEADING_RE = re.compile(r"^#{1,6}\s")
VALID_STATUSES = {"investigate", "todo", "doing", "review", "done"}


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
        fields["_done"] = done
        fields["_total"] = total
        fields["_kind"] = kind
        fields["_body"] = body
        items.append(fields)
    return items


def _read_config(backlog_dir: Path) -> dict:
    """Same optional, gitignored board.config.json the board server reads (see
    serve.py's own _read_config) — duplicated rather than imported, since build.py
    stays a standalone, dependency-free file that works without the plugin installed."""
    cfg = backlog_dir / "board.config.json"
    if not cfg.is_file():
        return {}
    try:
        return json.loads(cfg.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def configured_require_deferral_links(backlog_dir: Path) -> bool:
    """Read {"require_deferral_links": bool} from board.config.json. Defaults to
    False — off unless a project opts in, since forcing every unchecked subtask to
    name a filed task id is a real workflow constraint other projects may not want."""
    return bool(_read_config(backlog_dir).get("require_deferral_links", False))


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


def check_duplicate_ids(epics, tasks):
    """Two concurrent Claude sessions can each allocate the same next_id() before either
    writes its file — next_id() is a scan, not a reservation. No lock closes that race,
    so this catches it after the fact: a loud, named failure on the next build instead of
    two files silently sharing one id."""
    seen = {}
    for item in epics + tasks:
        item_id = item.get("id")
        if item_id in seen:
            raise SystemExit(
                f"backlog: duplicate id {item_id}: {seen[item_id]} vs {item['_path']} "
                f"— renumber one of them and re-run build.py"
            )
        seen[item_id] = item["_path"]


def check_known_status(epics, tasks):
    """The board only ever renders a task into one of five known status columns
    (app.js's STATUSES constant) — any other literal value (a typo, a status name
    from an older skill version like "blocked"/"needs-input", a different plugin's
    vocabulary like "in-progress") matches none of them and the card vanishes from
    every column with no error anywhere, on the live board or after a build. Same
    after-the-fact, named-failure shape as check_duplicate_ids: catch it here so
    there's a loud, actionable message instead of a task that's silently invisible."""
    for item in epics + tasks:
        status = item.get("status", "todo")
        if status not in VALID_STATUSES:
            raise SystemExit(
                f"backlog: {item.get('id', '?')} has status \"{status}\", which the "
                f"board doesn't recognize and won't render on any column — valid "
                f"values are {sorted(VALID_STATUSES)}. Fix the `status:` field in "
                f"{item['_path']} and re-run build.py"
            )


def _subtask_blocks(body):
    """Yield (checked, block_text) for each subtask: its checkbox line plus any
    indented continuation lines, up to the next checkbox or the next heading. A
    deferral note commonly wraps across lines, so a per-line regex alone can't see
    the whole subtask's text."""
    lines = body.splitlines()
    blocks = []
    current = None
    for line in lines:
        m = SUBTASK_LINE_RE.match(line)
        if m:
            if current is not None:
                blocks.append(current)
            current = {"checked": m.group(2).lower() == "x", "lines": [line]}
        elif current is not None:
            if HEADING_RE.match(line) or SUBTASK_LINE_RE.match(line):
                blocks.append(current)
                current = None
            elif line.strip() == "" and not line.startswith((" ", "\t")):
                blocks.append(current)
                current = None
            else:
                current["lines"].append(line)
    if current is not None:
        blocks.append(current)
    return [(b["checked"], "\n".join(b["lines"])) for b in blocks]


def check_deferred_subtasks(tasks):
    """Opt-in (see configured_require_deferral_links): a done/review task may keep an
    unchecked subtask only if that subtask names a filed task id — otherwise the
    deferred work has no home and quietly falls off the board (a task can reach done/
    review with an honest, explained deferral in its Notes, and nothing ever files the
    follow-up). `review` is included, not just `done`, because the board's own
    promote_stale_review sweep auto-promotes review -> done with no subtask check after
    review_after_days — review is the last point a human is still looking. Same
    after-the-fact, named-failure shape as check_duplicate_ids: this can't stop a stale
    board sweep from running, but it stops the next `build.py` from staying quiet
    about the result."""
    ids = {t.get("id") for t in tasks}
    for t in tasks:
        if t.get("status") not in ("done", "review"):
            continue
        own_id = t.get("id")
        for checked, block in _subtask_blocks(t.get("_body", "")):
            if checked:
                continue
            refs = {r for r in DEFERRAL_REF_RE.findall(block) if r != own_id}
            if refs & ids:
                continue
            snippet = block.strip().splitlines()[0][:80]
            raise SystemExit(
                f"backlog: {own_id} is {t.get('status')} with an unchecked subtask "
                f"that names no filed task id: \"{snippet}\" ({t['_path']}) "
                f"— file a follow-up task and reference its ID in the subtask, "
                f"or check the box"
            )


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


def build_index(epics, tasks, backlog_dir: Path):
    lines = ["<!-- generated by build.py — do not edit -->",
             f"next: {next_id(tasks, 'T', next_id_floor(backlog_dir, 'T'))} · "
             f"{next_id(epics, 'E', next_id_floor(backlog_dir, 'E'))}", ""]
    lines += ["| ID | Title | Epic | Status | Pri | Done |",
              "|---|---|---|---|---|---|"]
    for t in sorted(tasks, key=lambda t: t.get("id", "")):
        done, total = t["_done"], t["_total"]
        progress = f"{done}/{total}" if total else "—"
        lines.append(
            f"| {t.get('id', '')} | {t.get('title', '')} | {t.get('epic', '')} | "
            f"{t.get('status', 'todo')} | {t.get('priority', '')} | {progress} |"
        )
    return "\n".join(lines) + "\n"


def archive(backlog_dir: Path, epics, tasks):
    """Delete status:done tasks, and epics whose derived status is done. Git history is
    the archive: a file is only removed if it is tracked and clean, so `git show` can
    always recover it. Opt-in (--archive) rather than automatic on every build."""
    removed = []
    for item in tasks + epics:
        if item.get("status") != "done":
            continue
        src = Path(item["_path"])
        if not git_clean_tracked(src):
            continue
        src.unlink()
        removed.append(src)
    remove_empty_archive_dirs(backlog_dir)
    return removed


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
    — read-check-append only, that file is shared project territory, never rewritten."""
    gi = backlog_dir / ".gitignore"
    if not gi.is_file() or gi.read_text() != GITIGNORE_CONTENT:
        gi.write_text(GITIGNORE_CONTENT)

    marker = project_root / ".backlogrc.json"
    if marker.is_file():
        root_gi = project_root / ".gitignore"
        existing = root_gi.read_text() if root_gi.is_file() else ""
        if ".backlogrc.json" not in existing:
            sep = "" if not existing or existing.endswith("\n") else "\n"
            with root_gi.open("a") as f:
                f.write(f"{sep}.backlogrc.json\n")


def run(backlog_dir: Path):
    ensure_gitignore(backlog_dir, backlog_dir.parent)
    epics = load_items(backlog_dir / "epics", "epic")
    tasks = load_items(backlog_dir / "tasks", "task")
    check_duplicate_ids(epics, tasks)
    check_known_status(epics, tasks)
    if configured_require_deferral_links(backlog_dir):
        check_deferred_subtasks(tasks)
    for e in epics:
        e["status"] = derive_epic_status(e, tasks)
    (backlog_dir / "INDEX.md").write_text(build_index(epics, tasks, backlog_dir))
    print(f"wrote {backlog_dir / 'INDEX.md'} ({len(epics)} epics, {len(tasks)} tasks)")


def run_archive(backlog_dir: Path):
    epics = load_items(backlog_dir / "epics", "epic")
    tasks = load_items(backlog_dir / "tasks", "task")
    for e in epics:
        e["status"] = derive_epic_status(e, tasks)
    removed = archive(backlog_dir, epics, tasks)
    for path in removed:
        print(f"deleted {path} (recover with git show)")
    if not removed:
        print("nothing to delete")
    run(backlog_dir)


def selftest():
    fm, body = parse_frontmatter(
        "---\nid: T-001\nstatus: todo\n---\n## Subtasks\n- [x] a\n- [ ] b\n"
    )
    assert fm == {"id": "T-001", "status": "todo"}, fm
    assert checkbox_progress(body) == (1, 2)

    fm2, _ = parse_frontmatter("no frontmatter here")
    assert fm2 == {}

    items = [{"id": "T-001"}, {"id": "T-003"}]
    assert next_id(items, "T") == "T-004"
    assert next_id([], "E") == "E-001"

    epic = {"id": "E-001"}
    tasks = [{"epic": "E-001", "status": "doing"}, {"epic": "E-001", "status": "todo"}]
    assert derive_epic_status(epic, tasks) == "doing"
    tasks_done = [{"epic": "E-001", "status": "done"}]
    assert derive_epic_status(epic, tasks_done) == "done"
    assert derive_epic_status(epic, []) == "todo"
    # investigate: outranks todo, but doing still wins over it
    tasks_investigate = [{"epic": "E-001", "status": "investigate"}, {"epic": "E-001", "status": "todo"}]
    assert derive_epic_status(epic, tasks_investigate) == "investigate"
    tasks_doing_and_investigate = [{"epic": "E-001", "status": "doing"},
                                    {"epic": "E-001", "status": "investigate"}]
    assert derive_epic_status(epic, tasks_doing_and_investigate) == "doing"
    # task-less epic can carry investigate directly on its own frontmatter
    assert derive_epic_status({"id": "E-001", "status": "investigate"}, []) == "investigate"
    # review: every task accepted or awaiting acceptance, none still in flight
    tasks_review = [{"epic": "E-001", "status": "review"}, {"epic": "E-001", "status": "done"}]
    assert derive_epic_status(epic, tasks_review) == "review"
    tasks_doing_and_review = [{"epic": "E-001", "status": "doing"},
                               {"epic": "E-001", "status": "review"}]
    assert derive_epic_status(epic, tasks_doing_and_review) == "doing"

    check_duplicate_ids([], [{"id": "T-001", "_path": "a"}, {"id": "T-002", "_path": "b"}])
    try:
        check_duplicate_ids([], [{"id": "T-001", "_path": "a"}, {"id": "T-001", "_path": "b"}])
        raise AssertionError("expected SystemExit on duplicate id")
    except SystemExit as exc:
        assert "T-001" in str(exc) and "a" in str(exc) and "b" in str(exc), exc

    # check_known_status: every valid status passes for both epics and tasks
    check_known_status(
        [{"id": "E-001", "status": "investigate", "_path": "e"}],
        [{"id": f"T-{i:03d}", "status": s, "_path": "t"}
         for i, s in enumerate(sorted(VALID_STATUSES))],
    )
    # a status the board can't render (typo, or another plugin's vocabulary) raises,
    # naming the offending id
    try:
        check_known_status([], [{"id": "T-005", "status": "in-progress", "_path": "t"}])
        raise AssertionError("expected SystemExit on unrecognized status")
    except SystemExit as exc:
        assert "T-005" in str(exc) and "in-progress" in str(exc), exc
    # same check applies to a task-less epic's own explicit status
    try:
        check_known_status([{"id": "E-002", "status": "blocked", "_path": "e"}], [])
        raise AssertionError("expected SystemExit on unrecognized epic status")
    except SystemExit as exc:
        assert "E-002" in str(exc) and "blocked" in str(exc), exc

    # check_deferred_subtasks: done/review task with a bare unchecked subtask -> raises
    bare = {"id": "T-010", "status": "done", "_path": "a",
            "_body": "## Subtasks\n- [ ] do the thing\n"}
    try:
        check_deferred_subtasks([bare])
        raise AssertionError("expected SystemExit on unreferenced deferred subtask")
    except SystemExit as exc:
        assert "T-010" in str(exc), exc

    # ...unless the subtask names a filed task id, and that id exists
    referenced = {"id": "T-010", "status": "done", "_path": "a",
                  "_body": "## Subtasks\n- [ ] do the thing\n      — deferred, filed as [[T-011]]\n"}
    followup = {"id": "T-011", "status": "todo", "_path": "b", "_body": ""}
    check_deferred_subtasks([referenced, followup])  # no raise

    # ...referencing an id that isn't actually filed still raises
    dangling = {"id": "T-010", "status": "done", "_path": "a",
                "_body": "## Subtasks\n- [ ] do the thing — see T-999\n"}
    try:
        check_deferred_subtasks([dangling])
        raise AssertionError("expected SystemExit on reference to unfiled id")
    except SystemExit as exc:
        assert "T-010" in str(exc), exc

    # todo tasks with unchecked subtasks are untouched regardless
    todo_open = {"id": "T-012", "status": "todo", "_path": "c",
                 "_body": "## Subtasks\n- [ ] not started\n"}
    check_deferred_subtasks([todo_open])  # no raise

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        backlog_dir = project_root / "backlog"
        backlog_dir.mkdir()
        (backlog_dir / "tasks").mkdir()
        (backlog_dir / "epics").mkdir()
        (backlog_dir / "tasks" / "T-001-x.md").write_text(
            "---\nid: T-001\nepic: E-001\nstatus: done\n---\n- [x] a\n"
        )
        (backlog_dir / "tasks" / "T-002-x.md").write_text(
            "---\nid: T-002\nepic: E-001\nstatus: todo\n---\n- [ ] a\n"
        )
        (backlog_dir / "epics" / "E-001-x.md").write_text("---\nid: E-001\n---\n")

        run_tasks = load_items(backlog_dir / "tasks", "task")
        run_epics = load_items(backlog_dir / "epics", "epic")
        for e in run_epics:
            e["status"] = derive_epic_status(e, run_tasks)
        assert run_epics[0]["status"] == "todo"  # T-002 still open, epic not archivable

        (backlog_dir / "INDEX.md").write_text("next: T-003 · E-002\n")
        # outside a git repo nothing is deleted
        assert archive(backlog_dir, run_epics, run_tasks) == []
        assert (backlog_dir / "tasks" / "T-001-x.md").exists()

        import subprocess as sp
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            sp.run(["git", "-C", tmp, *cmd], check=True, capture_output=True)
        removed = archive(backlog_dir, run_epics, run_tasks)
        assert len(removed) == 1 and removed[0].name == "T-001-x.md", removed
        assert not (backlog_dir / "tasks" / "T-001-x.md").exists()
        assert archive(backlog_dir, run_epics, load_items(backlog_dir / "tasks", "task")) == []

        # deleted id must not be reissued
        assert next_id(load_items(backlog_dir / "tasks", "task"), "T",
                       next_id_floor(backlog_dir, "T")) == "T-003"

        run(backlog_dir)
        assert "next:" in (backlog_dir / "INDEX.md").read_text()

        # require_deferral_links defaults to off: a done task with a bare unchecked
        # subtask doesn't stop run() unless the project opts in via board.config.json
        assert configured_require_deferral_links(backlog_dir) is False
        (backlog_dir / "tasks" / "T-003-x.md").write_text(
            "---\nid: T-003\nepic: E-001\nstatus: done\n---\n- [ ] deferred, unlinked\n"
        )
        run(backlog_dir)  # no raise — opt-in check is off by default
        (backlog_dir / "board.config.json").write_text('{"require_deferral_links": true}')
        assert configured_require_deferral_links(backlog_dir) is True
        try:
            run(backlog_dir)
            raise AssertionError("expected SystemExit once require_deferral_links is on")
        except SystemExit as exc:
            assert "T-003" in str(exc), exc
        (backlog_dir / "tasks" / "T-003-x.md").unlink()
        (backlog_dir / "board.config.json").unlink()

        # run() calls ensure_gitignore — backlog/.gitignore exists with the exact content
        gi_path = backlog_dir / ".gitignore"
        assert gi_path.read_text() == GITIGNORE_CONTENT
        mtime_before = gi_path.stat().st_mtime_ns
        run(backlog_dir)
        assert gi_path.stat().st_mtime_ns == mtime_before  # unchanged content -> no rewrite

        # .backlogrc.json at the project root -> exactly one line appended to root .gitignore,
        # existing unrelated content left untouched, repeat calls don't duplicate it
        (project_root / ".gitignore").write_text("some_other_tool_output/\n")
        (project_root / ".backlogrc.json").write_text('{"dir": "backlog"}')
        ensure_gitignore(backlog_dir, project_root)
        root_gi_text = (project_root / ".gitignore").read_text()
        assert "some_other_tool_output/" in root_gi_text
        assert root_gi_text.count(".backlogrc.json") == 1, root_gi_text
        ensure_gitignore(backlog_dir, project_root)
        assert (project_root / ".gitignore").read_text().count(".backlogrc.json") == 1

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--archive" in sys.argv:
        run_archive(Path(__file__).resolve().parent)
    else:
        run(Path(__file__).resolve().parent)
