import subprocess
from datetime import date, timedelta
from pathlib import Path


def test_parsers_agree(build, serve):
    text = "---\nid: T-001\nstatus: inprogress # x\npriority: P2\n---\n- [x] a\n- [ ] b\n"
    assert build.parse_frontmatter(text) == serve.parse_frontmatter(text)
    assert build.checkbox_progress("- [x] a\n- [ ] b") == serve.checkbox_progress("- [x] a\n- [ ] b")


def test_epic_status_derivation_agrees(build, serve):
    for statuses in (["done"], ["doing", "todo"], ["investigate", "todo"],
                     ["done", "review"], ["todo"], []):
        tasks = [{"epic": "E-1", "status": s} for s in statuses]
        epic = {"id": "E-1"}
        assert build.derive_epic_status(epic, tasks) == serve.derive_epic_status(epic, tasks)


def test_inprogress_alias(serve, backlog, put):
    put(backlog, "tasks", "T-001-a.md", id="T-001", status="inprogress")
    assert serve.load_items(backlog / "tasks", "task")[0]["status"] == "todo"


def test_business_days_skip_weekends(serve):
    fri, mon = date(2026, 8, 14), date(2026, 8, 17)
    assert serve._business_days_since(fri, mon) == 1
    assert serve._business_days_since(fri, fri) == 0
    assert serve._business_days_since(date(2026, 8, 10), date(2026, 8, 17)) == 5


def test_sync_build_py_overwrites_stale_copy(serve, backlog):
    (backlog / "build.py").write_text("stale")
    serve.sync_build_py(backlog)
    plugin_copy = Path(serve.__file__).resolve().parent.parent / "skills/backlog/build.py"
    assert (backlog / "build.py").read_bytes() == plugin_copy.read_bytes()


def _old(days=30):
    return (date.today() - timedelta(days=days)).isoformat()


def test_retention_deletes_old_done_and_commits(serve, backlog, put, git_repo):
    put(backlog, "tasks", "T-001-old.md", id="T-001", status="done", completed=_old())
    put(backlog, "tasks", "T-002-new.md", id="T-002", status="done", completed=_old(1))
    put(backlog, "tasks", "T-003-open.md", id="T-003", status="todo")
    git_repo("add", "-A")
    git_repo("commit", "-qm", "init")

    serve._run_sweeps(backlog)

    assert not (backlog / "tasks/T-001-old.md").exists()
    assert (backlog / "tasks/T-002-new.md").exists()  # inside the retention window
    assert (backlog / "tasks/T-003-open.md").exists()
    assert git_repo("log", "-1", "--format=%s") == f"Backlog tasks update {date.today().isoformat()}"
    assert git_repo("status", "--porcelain") == ""


def test_retention_skips_dirty_and_untracked(serve, backlog, put, git_repo):
    put(backlog, "tasks", "T-001-a.md", id="T-001", status="done", completed=_old())
    git_repo("add", "-A")
    git_repo("commit", "-qm", "init")
    (backlog / "tasks/T-001-a.md").write_text(
        f"---\nid: T-001\nstatus: done\ncompleted: {_old()}\n---\nedited\n")  # uncommitted
    put(backlog, "tasks", "T-002-b.md", id="T-002", status="done", completed=_old())  # untracked
    assert serve.archive_stale_done(backlog, 7) == []


def test_retention_respects_config(serve, backlog, put, git_repo):
    put(backlog, "tasks", "T-001-a.md", id="T-001", status="done", completed=_old(30))
    (backlog / "board.config.json").write_text('{"archive_enabled": false}')
    git_repo("add", "-A", "-f")
    git_repo("commit", "-qm", "init")
    serve._run_sweeps(backlog)
    assert (backlog / "tasks/T-001-a.md").exists()

    (backlog / "board.config.json").write_text('{"archive_after_days": 3, "auto_commit": false}')
    serve._run_sweeps(backlog)
    assert not (backlog / "tasks/T-001-a.md").exists()
    assert git_repo("log", "-1", "--format=%s") == "init"  # auto_commit off -> no commit


def test_review_auto_accept_promotes_stale_review(serve, backlog, put):
    put(backlog, "tasks", "T-001-a.md", id="T-001", status="review", updated=_old(10))
    put(backlog, "tasks", "T-002-b.md", id="T-002", status="review",
                  updated=date.today().isoformat())
    assert serve.promote_stale_review(backlog, 1) == ["T-001"]
    items = {i["id"]: i for i in serve.load_items(backlog / "tasks", "task")}
    assert items["T-001"]["status"] == "done"
    assert items["T-001"]["completed"] == date.today().isoformat()
    assert items["T-002"]["status"] == "review"


def test_inline_selftests_still_pass():
    root = Path(__file__).resolve().parent.parent
    for script in ("skills/backlog/build.py", "server/serve.py"):
        r = subprocess.run(["python3", str(root / script), "--selftest"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_load_items_sorted_numerically(serve, backlog, put):
    for i in (1000000, 9, 1000, 10):
        put(backlog, "tasks", f"T-{i}-x.md", id=f"T-{i}", status="todo")
    items = serve.load_items(backlog / "tasks", "task")
    assert [t["id"] for t in items] == ["T-9", "T-10", "T-1000", "T-1000000"]


def test_sort_key_parity(build, serve):
    items = [{"id": "T-9"}, {"id": "T-1000"}, {"id": "T-k7f2m", "created": "2026-11-19T22:10"},
             {"id": "T-10", "created": "2026-11-19"}]
    assert sorted(items, key=build._sort_key) == sorted(items, key=serve._sort_key)


def test_refresh_index_rebuilds_and_reports_errors(serve, backlog, put, capsys):
    serve.refresh_index(backlog)  # no build.py -> silent no-op
    serve.sync_build_py(backlog)
    put(backlog, "epics", "E-001-x.md", id="E-001", title="X")
    put(backlog, "tasks", "T-k7f2m-a.md", id="T-k7f2m", title="a", epic="E-001", status="todo")
    serve.refresh_index(backlog)
    assert "T-k7f2m" in (backlog / "INDEX.md").read_text()
    assert capsys.readouterr().out == ""
    put(backlog, "tasks", "T-k7f2m-b.md", id="T-k7f2m", title="dup", epic="E-001", status="todo")
    serve.refresh_index(backlog)
    assert "duplicate id" in capsys.readouterr().out


def _setup_old_done(put, backlog, git_repo):
    put(backlog, "tasks", "T-001-old.md", id="T-001", status="done", completed=_old())
    git_repo("add", "-A")
    git_repo("commit", "-qm", "init")


def test_sweep_skipped_on_feature_branch_by_default(serve, backlog, put, git_repo):
    _setup_old_done(put, backlog, git_repo)
    git_repo("checkout", "-qb", "feature")
    serve._run_sweeps(backlog)
    assert (backlog / "tasks/T-001-old.md").exists()
    assert git_repo("log", "-1", "--format=%s") == "init"


def test_branch_allow_list_in_config(serve, backlog, put, git_repo):
    _setup_old_done(put, backlog, git_repo)
    git_repo("checkout", "-qb", "feature")
    (backlog / "board.config.json").write_text('{"auto_commit_branches": ["feature"]}')
    serve._run_sweeps(backlog)
    assert not (backlog / "tasks/T-001-old.md").exists()
    assert git_repo("log", "-1", "--format=%s").startswith("Backlog tasks update")


def test_wildcard_allows_any_branch(serve, backlog, put, git_repo):
    _setup_old_done(put, backlog, git_repo)
    git_repo("checkout", "-qb", "whatever")
    (backlog / "board.config.json").write_text('{"auto_commit_branches": "*"}')
    serve._run_sweeps(backlog)
    assert not (backlog / "tasks/T-001-old.md").exists()


def test_sweep_skipped_mid_rebase_and_detached_head(serve, backlog, put, git_repo, tmp_path):
    _setup_old_done(put, backlog, git_repo)
    (tmp_path / ".git" / "rebase-merge").mkdir()  # what git leaves during a rebase
    serve._run_sweeps(backlog)
    assert (backlog / "tasks/T-001-old.md").exists()
    (tmp_path / ".git" / "rebase-merge").rmdir()

    git_repo("checkout", "-q", "--detach")
    serve._run_sweeps(backlog)
    assert (backlog / "tasks/T-001-old.md").exists()


def test_review_promotion_also_paused_mid_merge(serve, backlog, put, git_repo, tmp_path):
    put(backlog, "tasks", "T-001-r.md", id="T-001", status="review", updated=_old(10))
    git_repo("add", "-A")
    git_repo("commit", "-qm", "init")
    (tmp_path / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    serve._run_sweeps(backlog)
    assert serve.load_items(backlog / "tasks", "task")[0]["status"] == "review"


def test_commit_failure_is_reported(serve, backlog, put, git_repo, monkeypatch, capsys):
    _setup_old_done(put, backlog, git_repo)
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    serve._run_sweeps(backlog)
    out = capsys.readouterr().out
    assert "WARNING" in out and "could not be committed" in out
