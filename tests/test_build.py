import pytest


def test_parse_frontmatter_and_checkboxes(build):
    fm, body = build.parse_frontmatter(
        "---\nid: T-001\nstatus: todo # trailing comment\n---\n- [x] a\n- [ ] b\n")
    assert fm == {"id": "T-001", "status": "todo"}
    assert build.checkbox_progress(body) == (1, 2)


def test_parse_frontmatter_missing(build):
    assert build.parse_frontmatter("just text")[0] == {}


def test_duplicate_ids_fail(build):
    a = {"id": "T-001", "_path": "a.md"}
    b = {"id": "T-001", "_path": "b.md"}
    with pytest.raises(SystemExit, match="duplicate id T-001"):
        build.check_duplicate_ids([], [a, b])


def test_unknown_status_fails_but_inprogress_is_valid(build, backlog, put):
    put(backlog, "tasks", "T-001-a.md", id="T-001", epic="E-001", status="blocked")
    tasks = build.load_items(backlog / "tasks", "task")
    with pytest.raises(SystemExit, match="blocked"):
        build.check_known_status([], tasks)

    put(backlog, "tasks", "T-001-a.md", id="T-001", epic="E-001", status="inprogress")
    tasks = build.load_items(backlog / "tasks", "task")
    build.check_known_status([], tasks)
    assert tasks[0]["status"] == "todo"  # alias


@pytest.mark.parametrize("statuses,expected", [
    (["done", "done"], "done"),
    (["doing", "todo"], "doing"),
    (["investigate", "todo"], "investigate"),
    (["doing", "investigate"], "doing"),
    (["done", "review"], "review"),
    (["done", "todo"], "todo"),
])
def test_derive_epic_status(build, statuses, expected):
    tasks = [{"epic": "E-001", "status": s} for s in statuses]
    assert build.derive_epic_status({"id": "E-001"}, tasks) == expected


def test_taskless_epic_keeps_own_status(build):
    assert build.derive_epic_status({"id": "E-001", "status": "investigate"}, []) == "investigate"
    assert build.derive_epic_status({"id": "E-001"}, []) == "todo"


def test_deferral_gate(build):
    def task(tid, status, body):
        return {"id": tid, "status": status, "_path": f"{tid}.md", "_body": body}

    # unchecked subtask with no filed id -> refuse
    with pytest.raises(SystemExit, match="names no filed task id"):
        build.check_deferred_subtasks([task("T-001", "done", "- [ ] later\n")])
    # names another existing task -> fine
    build.check_deferred_subtasks([task("T-001", "done", "- [ ] later, see T-002\n"),
                                   task("T-002", "todo", "")])
    # a reference to itself, or to a task that doesn't exist, does not count
    with pytest.raises(SystemExit):
        build.check_deferred_subtasks([task("T-001", "review", "- [ ] x T-001\n")])
    with pytest.raises(SystemExit):
        build.check_deferred_subtasks([task("T-001", "done", "- [ ] x T-099\n")])
    # open tasks are untouched
    build.check_deferred_subtasks([task("T-001", "todo", "- [ ] later\n")])


def test_gate_is_opt_in(build, backlog, put):
    put(backlog, "epics", "E-001-x.md", id="E-001", title="X")
    put(backlog, "tasks", "T-001-a.md", id="T-001", epic="E-001", status="done",
                  body="- [ ] deferred\n")
    build.run(backlog)  # default: off, builds fine
    (backlog / "board.config.json").write_text('{"require_deferral_links": true}')
    with pytest.raises(SystemExit):
        build.run(backlog)


def test_index_generation(build, backlog, put):
    put(backlog, "epics", "E-001-x.md", id="E-001", title="Epic one")
    put(backlog, "tasks", "T-001-a.md", id="T-001", title="First", epic="E-001",
                  status="doing", priority="P1", body="- [x] a\n- [ ] b\n")
    put(backlog, "tasks", "T-002-b.md", id="T-002", title="Second", epic="E-001",
                  status="todo", priority="P2")
    build.run(backlog)
    index = (backlog / "INDEX.md").read_text()
    assert "next:" not in index
    assert "| T-001 | First | E-001 | doing | P1 | 1/2 |" in index
    assert "| T-002 | Second | E-001 | todo | P2 |" in index


def test_build_writes_gitignore(build, backlog):
    build.run(backlog)
    assert (backlog / ".gitignore").read_text() == build.GITIGNORE_CONTENT


def test_archive_deletes_only_clean_tracked_done(build, backlog, put, git_repo):
    put(backlog, "epics", "E-001-x.md", id="E-001", title="X")
    put(backlog, "tasks", "T-001-a.md", id="T-001", epic="E-001", status="done")
    put(backlog, "tasks", "T-002-b.md", id="T-002", epic="E-001", status="todo")
    git_repo("add", "-A")
    git_repo("commit", "-qm", "init")
    epics = build.load_items(backlog / "epics", "epic")
    tasks = build.load_items(backlog / "tasks", "task")
    removed = build.archive(backlog, epics, tasks)
    assert [p.name for p in removed] == ["T-001-a.md"]
    assert (backlog / "tasks" / "T-002-b.md").exists()


@pytest.mark.parametrize("digits", [1, 3, 4, 7])
def test_ids_work_for_any_digit_count(build, backlog, put, digits):
    n = 10 ** (digits - 1)  # 1, 100, 1000, 1000000
    tid, other = f"T-{n}", f"T-{n + 1}"

    # deferral gate: reference matches the whole id, not a 3-digit prefix of it
    def task(i, status, body=""):
        return {"id": i, "status": status, "_path": f"{i}.md", "_body": body}

    build.check_deferred_subtasks([task(tid, "done", f"- [ ] later, see {other}\n"), task(other, "todo")])
    with pytest.raises(SystemExit):  # T-100 must not be satisfied by a reference to T-1000
        build.check_deferred_subtasks([task("T-5", "done", f"- [ ] see T-{n}0\n"), task(tid, "todo")])


def test_index_sorts_numerically(build, backlog, put):
    ids = [9, 10, 101, 1000, 1000000]
    for i in reversed(ids):
        put(backlog, "tasks", f"T-{i}-x.md", id=f"T-{i}", title="t", epic="E-001", status="todo")
    put(backlog, "epics", "E-001-x.md", id="E-001", title="X")
    build.run(backlog)
    rows = [ln.split("|")[1].strip() for ln in (backlog / "INDEX.md").read_text().splitlines()
            if ln.startswith("| T-")]
    assert rows == [f"T-{i}" for i in ids]


def test_new_id_format_and_uniqueness(build):
    import re
    ids = {build.new_id([], "T") for _ in range(200)}
    assert len(ids) == 200  # no collisions in a small sample
    assert all(re.fullmatch(r"T-[a-z0-9]{7}", i) for i in ids)
    assert build.new_id([], "E").startswith("E-")


def test_new_id_retries_on_clash(build):
    class Rng:  # first candidate collides with an existing id, second is free
        def __init__(self):
            self.seq = iter("2222222" + "3333333")

        def choice(self, _alphabet):
            return next(self.seq)

    assert build.new_id([{"id": "T-2222222"}], "T", rng=Rng()) == "T-3333333"


def test_mixed_legacy_and_random_ids_sort_by_created(build, backlog, put):
    put(backlog, "epics", "E-001-x.md", id="E-001", title="X")
    put(backlog, "tasks", "T-400-a.md", id="T-400", title="legacy", epic="E-001",
        status="todo", created="2026-11-01")
    put(backlog, "tasks", "T-k7f2m-b.md", id="T-k7f2m", title="newer", epic="E-001",
        status="todo", created="2026-11-19T22:10:03")
    put(backlog, "tasks", "T-bbbbb-c.md", id="T-bbbbb", title="newest", epic="E-001",
        status="todo", created="2026-11-19T23:00:00")
    build.run(backlog)
    rows = [ln.split("|")[1].strip() for ln in (backlog / "INDEX.md").read_text().splitlines()
            if ln.startswith("| T-")]
    assert rows == ["T-400", "T-k7f2m", "T-bbbbb"]


def test_deferral_gate_with_random_ids(build):
    def task(i, status, body=""):
        return {"id": i, "status": status, "_path": f"{i}.md", "_body": body}

    build.check_deferred_subtasks([task("T-k7f2m", "done", "- [ ] later, see T-bbbbb\n"), task("T-bbbbb", "todo")])
    with pytest.raises(SystemExit):
        build.check_deferred_subtasks([task("T-k7f2m", "done", "- [ ] later, see T-zzzzz\n")])


def test_new_cli_prints_id_and_created(build, backlog, capsys):
    build.run_new(backlog, ["task"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("id: T-") and lines[1].startswith("created: 20")
    assert "T" in lines[1]  # ISO datetime
    with pytest.raises(SystemExit, match="usage"):
        build.run_new(backlog, ["bogus"])


def test_index_md_is_gitignored(build, backlog, git_repo):
    import subprocess
    build.run(backlog)
    out = subprocess.run(["git", "check-ignore", "-q", str(backlog / "INDEX.md")],
                         cwd=backlog.parent, check=False)
    assert out.returncode == 0
    keep = subprocess.run(["git", "check-ignore", "-q", str(backlog / "ADR.md")],
                          cwd=backlog.parent, check=False)
    assert keep.returncode == 1  # hand-written markdown still tracked
