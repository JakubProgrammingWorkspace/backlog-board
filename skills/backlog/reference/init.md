# Bootstrapping backlog/ in a new project

Run once per project, from the project root.

**First check: does `backlog/` already exist in this project for something else** — a
queue, a metric, domain data unrelated to this dev-task tracker? If so, pick a different
name (e.g. `dev-tracker`) and write a `.backlogrc.json` at the project root *before* the
`mkdir` below, so every tool in this skill resolves the renamed dir automatically:

```bash
echo '{"dir": "dev-tracker"}' > .backlogrc.json
```

`.backlogrc.json` itself is never renamed — it's the one fixed, predictable name that
lets the board server (and anything else) find the (possibly renamed) data dir. Not
committed (see the gitignore policy below) — `build.py`/`serve.py` auto-append it to the
project's own root `.gitignore` the first time either runs, no manual step needed. The
rest of this doc uses `backlog/` as the example name; substitute your chosen name
throughout if you set this.

```bash
mkdir -p backlog/epics backlog/tasks
cp "$CLAUDE_PLUGIN_ROOT/skills/backlog/build.py" backlog/build.py
```

`build.py` is copied in, not referenced from the plugin directory, so it can be *run*
(`python3 backlog/build.py`) without the plugin installed. It's **not committed** —
see the gitignore policy below — the plugin is its canonical source, so a project
doesn't carry its own copy that can drift out of sync. A fresh clone or CI machine
without the plugin loses it until someone with the plugin reruns the `cp` above.

Then seed `backlog/ADR.md`:

```markdown
# Architecture Decision Records

Append-only, newest entry last. See templates.md in the backlog skill for the
entry format.
```

Then seed the bootstrap migration epic + task — every fresh project gets
these as `E-001`/`T-001` so there's always a tracked place for "surface
whatever this project was already tracking ad hoc":

```bash
cp "$CLAUDE_PLUGIN_ROOT/skills/backlog/reference/seed/E-001-migrate-existing-planning-docs.md" backlog/epics/
cp "$CLAUDE_PLUGIN_ROOT/skills/backlog/reference/seed/T-001-migrate-existing-planning-docs.md" backlog/tasks/
```

Replace the `YYYY-MM-DD` placeholders in both with today's date before
running the build. If `backlog/` already has an `E-001`/`T-001` (re-running
init on an already-bootstrapped project), pick the next free IDs from
`INDEX.md` instead of overwriting.

And generate the initial `INDEX.md`:

```bash
python3 backlog/build.py
```

Nothing else is required — no config file, no dependency install. `INDEX.md`
should be committed to git (it's small and diffs cleanly).

**Gitignore policy: only `.md` files under `backlog/` are ever committed** — `build.py`,
`board.config.json`, `.board`, everything non-`.md`, are excluded. This is enforced by
`backlog/.gitignore`, generated automatically (not something to create by hand) — both
`build.py` and the board server write/verify it every time they run, so it exists and
stays correct from the first `python3 backlog/build.py` or `/backlog-board`, on a brand
new project or one that's had `backlog/` for a while already. Nothing to do here.

Working `T-001` (the migration task) is now the first real piece of work:
scan the project for planning docs worth carrying over (a stale `plan.md`,
an ad-hoc open-issues doc, an old `ADR.md`, README TODOs), propose which
items look like real tasks, get a yes, and create task files for those.
Leave the rest of `docs/` alone — tasks can link to it via the task's
`docs:` frontmatter field instead of duplicating it. Once nothing's left to
migrate, mark `E-001` `status: done` and run `python3 backlog/build.py
--archive` to delete it (and `T-001`; git keeps them) — that's the epic-completion lifecycle this task exists to
demonstrate.
