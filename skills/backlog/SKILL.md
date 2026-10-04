---
name: backlog
description: Markdown-native epic/task/ADR backlog with a live board server, for this project's own dev-work tracker (the local backlog/ directory). Use when the user wants to add a task, create an epic, ask "what's next", show/open the board, mark something done, move a task's status, or record a decision/ADR. Also use to check progress on planned work before starting unrelated changes. NOT for other senses of "backlog" that may exist in this project's own domain data or business vocabulary (e.g. a queue, a metric, a term from the problem domain) — those are content the project works with, not this dev-task tracker, even when the word matches.
---

# Backlog

Epics and tasks are markdown files under `backlog/`. `INDEX.md` is **generated** —
never hand-edit it, and never hand-edit its content into existence; run `build.py`
instead. The live board (see below) reads the task/epic files directly, not `INDEX.md`.

Only `.md` files here are ever committed to git — `build.py`, `board.config.json`,
`.board`, and anything else non-`.md` are gitignored (enforced by an auto-generated
`backlog/.gitignore`, see "Gitignore policy" below). Never fight this or propose
committing one of those; it's deliberate.

```
backlog/
  INDEX.md            # generated — read this first, it has the next free ID
  build.py            # run after any write under backlog/ — gitignored, not committed;
                      #   overwritten from the plugin on every session start, never hand-edit
  ADR.md              # append-only decision log
  board.config.json   # optional — {"port": N, "archive_after_days": N, "archive_enabled": bool,
                      #   "nudge_at_tokens": N, "nudge_every_tokens": N}, gitignored
  .gitignore          # generated — enforces the only-.md policy above
  .board              # gitignored, written by the running board server (pid, port)
  .board-sessions     # gitignored, pids of the Claude sessions with this project open
  epics/E-001-slug.md
  tasks/T-014-slug.md
```

No `backlog/` in this project yet? Read `reference/init.md` and set it up first.

If `backlog/` already means something else in this project (domain data, a queue,
unrelated to this dev-task tracker), the data dir can be renamed — see
`reference/init.md`'s `.backlogrc.json` step. Everything below still says `backlog/`
for readability; substitute the configured name if the project has renamed it.

## Reading state

Read `backlog/INDEX.md`. It has a `next: T-0xx · E-0xx` line (the next free IDs)
and one table row per task. Only open an individual task/epic `.md` file when you
need its full body (Context/Subtasks/Notes) — don't read every file to answer
"what's next".

## Task file

```markdown
---
id: T-014
title: Short imperative title
epic: E-002
status: todo          # investigate | todo | doing | review | done
priority: P1           # P1 urgent/blocking, P2 normal, P3 low/someday
created: 2026-08-11
updated: 2026-08-11
docs: docs/some-investigation.md#anchor   # optional, link to source doc
---

## Context
Why this exists.

## Subtasks
- [ ] first concrete step
- [ ] second

## Notes
Findings appended as work proceeds.
```

Epic files are the same minus `epic`/`priority`, plus `## Goal` and
`## Scope / Non-goals`. See `reference/templates.md` for both in full, plus the
ADR entry format.

## Rules

- Allocate new IDs from `INDEX.md`'s `next:` line. **Never renumber** an
  existing ID, even if a task is deleted.
- After **any** create/edit/delete under `backlog/`, run
  `python3 backlog/build.py` to regenerate `INDEX.md`. The live board (if running)
  picks up the change on its own within ~1s — no separate step for it.
- Subtasks are checkboxes in the task body, not separate files. Only split a
  subtask into its own task if it grows past roughly one session of work —
  keep the original as its epic (or its parent task, promoted).
- Epic `status` is derived by `build.py` from its tasks — don't set it by hand.
- **Never invent tasks or epics unprompted.** Propose them in chat, get a yes,
  then write the file. Same for status changes that aren't the literal thing
  asked (e.g. don't mark unrelated tasks `review` while closing one out, and
  never set `done` yourself unless the user has actually said the work is
  accepted) — except flipping the task actually being worked on to `doing`
  when it starts, to `investigate` when starting reveals it isn't actually
  actionable, and to `review` when it finishes, see below — all three expected
  automatically. **Also except** continuation tasks and incidental findings (see "Session
  boundaries" below) and the epic a task needs when none fits — write those
  without asking too, same reasoning: waiting for a yes is exactly what loses
  the finding when the session ends first.

## Recording decisions — ADR.md

`backlog/ADR.md` is append-only, newest entry last. Format is in
`reference/templates.md`. Two rules:

1. **Never append without an explicit yes** from the user — propose the entry,
   show its content, wait for confirmation.
2. **Know when to ask.** Raise "should this be an ADR?" when a decision:
   - changes a dependency, a storage format, or a file format
   - changes an API/contract shape
   - rejects an alternative for a reason a future reader would otherwise
     re-propose
   - is a deliberate accepted-defect or won't-fix
   - reverses a previous ADR

   Stay silent for: naming choices, file placement, single-file refactors, or
   anything trivially reversible. Most edits during a session hit none of
   these — don't ask by default.

## Gitignore policy

Only `.md` files under `backlog/` are ever committed. `build.py` (the plugin is its
canonical source — a committed copy would just drift), `board.config.json`, `.board`,
and anything else non-`.md` are excluded. Enforced by `backlog/.gitignore`, generated
automatically — both `build.py` and the board server write/verify it on every run, so
there's nothing to set up and nothing to remember. If a project renamed its data dir
via `.backlogrc.json`, that file is gitignored too (same reasoning as `.board` — the
mechanism auto-appends one line for it to the project's own root `.gitignore`, leaving
everything else already in that file untouched).

Don't hand-edit `backlog/.gitignore` — it gets overwritten back to the canonical
content on the next `build.py` run or server startup.

## The board

`/backlog-board` starts (or reuses) the plugin's live board server and prints its
`http://127.0.0.1:PORT/` URL — run this when the user asks to "see the board" or
"open the board", then report the URL back (open it if you have a way to). It's a
small stdlib-only Python HTTP server (`server/serve.py` in the plugin, never copied
into the project) that renders `backlog/epics/*.md`, `backlog/tasks/*.md`, and
`backlog/ADR.md` live: file changes push over SSE, no manual refresh or rebuild step.
It has search (id/title/body), epic and priority filters, and a detail view per
task/epic. Port defaults to 3201 — override per-project with `backlog/board.config.json`
(`{"port": N}`), useful if the default collides with another project's running board.

**The board is read-only** — it has no write endpoints. Moving a card, editing a
title, checking a subtask: none of that happens in the UI. Task changes still go
through this skill (edit the frontmatter/body, the board just reflects it). If a
user tries to interact with the board expecting write-back, tell them to ask you
(or edit the file) instead — that's by design, not a missing feature.

Re-running `/backlog-board` is safe — it detects an already-running server for the
same project and reuses it rather than starting a second one. The plugin's
`SessionStart` hook already runs it automatically, so this skill rarely needs to
run `/backlog-board` itself unless the user explicitly asks to see it.

The plugin's `SessionEnd` hook stops it again when Claude exits — but only if no
other session still has this project open. The same server is shared across every
Claude session and terminal tab here, so each start registers its `CLAUDE_PID` in
`backlog/.board-sessions` and each exit removes it; the last one out stops the
server. `/clear` is exempt (a `SessionStart` follows it straight away). None of
this is something to manage by hand — don't edit `.board-sessions`, and if the
user asks to shut the board down right now, run `serve.py --stop`, which is
unconditional and ignores the registry. There's no idle self-reap — the board
stays up until the hooks stop it.

A project without the plugin installed has no board — it still has `INDEX.md` and
the raw `.md` files, which is why `build.py` stays dependency-free and copied into
each project (see `reference/init.md`).

## Starting a task

When work on a task actually begins — the point of editing files, running
commands, or otherwise doing the task, not just discussing or picking it —
set `status: doing` **and `session_id: $CLAUDE_CODE_SESSION_ID`** on that task
file (that env var is already in your shell — `echo "$CLAUDE_CODE_SESSION_ID"`
via Bash if you need to read it explicitly), then run `python3 backlog/build.py`.
Do this without asking first; it's the literal action of starting the work
the user just asked for, not an invented change (see the Rules exception
above). If a different task was already `doing`, leave it as-is — don't
silently flip it back to `todo`, that's the kind of unrelated status change
to ask about instead of assuming.

`session_id:` records which session is (or was last) working the task — the
same signal the "more than 2 `doing`" worktree rule below reads to reason
about concurrent sessions, and lets anyone glance at a task and tell whether
it's this session's or a different one's in-flight work before touching it.
Overwrite it every time a task is picked up, including by the same session
resuming it — it should always reflect who's currently on it, not history.

**More than 2 tasks `doing` at once → isolate in a git worktree.** Before
flipping status, count current `status: doing` rows in `INDEX.md`. If this
task would be the 3rd (or beyond) concurrently `doing`, do its work in a
separate git worktree instead of the main checkout — two or more tasks
already in flight in the shared working tree is exactly the case where a
third one's uncommitted changes collide with theirs. Use Claude Code's
built-in worktree isolation (`EnterWorktree`) rather than hand-rolling
`git worktree add`. The first two concurrent tasks can still share the main
checkout as today.

## When a task or epic isn't ready

Sometimes a task or epic gets written down before it's actually refined: the scope
depends on a decision only the user can make, an issue that needs investigating in
the code, a fact that needs a websearch, or something concrete and *external* is
stopping otherwise-understood work (another task, a missing credential, a vendor
fix). That's not `todo` (todo means understood and ready to pick up).

Set `status: investigate` instead, and say why in the task's `## Notes` (or the
epic's `## Context`, or — for a task-less epic — directly in its own frontmatter
`status:`, same fallback path that already lets a task-less epic set `todo` by
hand). Do this without asking first, same standing exception as `doing`/`review`:
discovering mid-start that a task isn't actually actionable is part of the literal
work of starting it, not an invented change.

Resolving it means actually getting the answer — from the user, from investigating,
from a websearch — not re-reading the task and guessing. Once resolved, fold the
answer into `## Context` (not just `## Notes`, so the task file stays self-contained
for whoever works it next) and flip to `todo`, or straight to `doing` if picking it
up immediately.

If that dig is wide, offer to delegate it — see "Session boundaries".

## Finishing a task — status: review

`done` is the human's word, not the agent's — it means "Jakub looked at this and
accepted it," not "the agent believes it's finished." So when the task's work is
actually finished, check off any subtask boxes still unchecked and set
**`status: review`** (not `done`), then run `python3 backlog/build.py`. Do this
without asking first — same standing exception as starting a task (see Rules
above): it's the literal completion of the work just asked for, not an invented
change. Say in chat that it's ready for review, in one line.

`review` is not a request for permission to keep going — the work is done, this is
just the acceptance step. Don't block on it before moving to the next task.

**Deferring a subtask instead of finishing it.** Sometimes a subtask turns out to
be out of scope for the task actually in hand — a tangential bug found along the
way, a follow-up measurement that only makes sense once this lands, a harder
variant deliberately left for later. That subtask can go to `review`/`done`
unchecked, but only with a reason: say why in `## Notes`, and if it's substantial
enough to need its own tracked work, file it immediately as its own task (see
"Incidental finding" below) and reference that task's id in the subtask line
itself — otherwise the deferral has no home and quietly falls off the board the
moment this task is accepted. A project can make this a hard build-time check —
`require_deferral_links: true` in `board.config.json` — which makes `build.py`
refuse to build if a `done`/`review` task has an unchecked subtask naming no
filed task id. Off by default; see the Configuration reference in the README.

## Accepting a task or epic — status: done

Setting `status: done` **and `completed: YYYY-MM-DD`** (today) is how a human
accepts a `review` task — do this yourself only when the user has actually looked
at the work and said so; it is not something to set on their behalf just because a
task reached `review`. The epic's own status is derived, not set by hand — it
flips to `done` automatically once every one of its tasks is `done` (and to
`review` once every one of its tasks is `done` or `review`, with at least one
`review` — see `derive_epic_status` in `build.py`/`serve.py`).

If the task turns out only partially done, or stuck on something external,
that's a different call from `review` — set `investigate` (or leave `doing`)
and say why, don't default to `review` just because work stopped.

**The board server auto-accepts a stale `review`, on a timer.** A `review` task
left untouched — nobody edits its `updated:` — auto-promotes to `done` (with
`completed:` stamped by the sweep itself, not hand-set) after a window — default
**1 working day** (weekends excluded), measured from `updated:` on purpose: a
human appending review notes bumps that field and buys another day, which is
the point — the clock only advances on actual silence. This is a deliberate
trade-off, not a safety net: it caps how long a card can sit unreviewed, at the
cost that unreviewed work still reaches `done` on its own one working day later.
Override the window per-project in `board.config.json`:

```json
{ "review_after_days": 3 }
```

Turn auto-accept off entirely with `{ "review_auto_accept": false }` in the same
file — `review` then becomes a hard gate that only a human clearing it (or a
later ADR reversing this default) can clear. Defaults to enabled when unset.

Both sweeps below run together, in the same pass, on the same schedule (server
startup, then once per calendar day the server stays up — see "The board"
above) — promotion runs first, so a task promoted this pass gets `completed:`
stamped today and can't also be deleted in that same pass.

Done items are **deleted, not archived — git history is the archive.** Run
`python3 backlog/build.py --archive` to delete every `status: done` task (and every
epic whose derived status is `done`) now, then rebuild. A file is only deleted if it
is git-tracked and clean (nothing uncommitted would be lost); otherwise it is skipped.
IDs are never reused: `INDEX.md` is committed and its `next:` line is the counter.

To read a deleted item: `git log --all --diff-filter=D --name-only -- 'backlog/tasks/T-051-*'`
finds the deleting commit, then `git show <sha>^:backlog/tasks/<file>`. Commit
messages are the other index (`git log --grep T-051`) — so name the task ID in the
commit message.

**Never put task or epic IDs in code, comments, docstrings, test names or docs
prose.** The task file will be deleted, leaving a dead pointer. Write the *why*
itself in the comment. IDs live in `docs:`/`[[T-xxx]]` links inside the backlog and
in commit messages, nowhere else.

**The board server does this automatically too, on the same schedule as the
review sweep above.** It deletes `done` items older than a retention window —
default **3 working days** (weekends excluded from the count), measured from
`completed:` (an epic with no `completed:`/`updated:` of its own uses the latest
`completed:` among its tasks, since epic status is derived, not hand-set).
`completed:` is always populated now — either by a human accepting the task, or
by the auto-accept sweep — so this no longer silently falls back to `updated:`
in the common case; that fallback still exists for tasks written before this
convention, or hand-edited outside it. Override the window per-project in
`board.config.json`:

```json
{ "archive_after_days": 7 }
```

Turn the whole sweep off per-project with `{ "archive_enabled": false }` in the same
file — the manual `--archive` above still works, only the automatic startup sweep is
gated by this flag. Defaults to enabled when unset.

This is a *separate* trigger from the manual `--archive` above — that one is
immediate and unconditional, this one is a background policy gated by age. Both
delete (with the same git-clean guard).

## Session boundaries

Long sessions grow context; that costs more and thinks worse. Working rule:
**one task per session.** No tool or hook can start or clear a session for
you — `/clear` is a keystroke only the user can make — so the plugin's job at
a boundary is to write a self-contained task file and hand over a way to
resume, not to cross the boundary itself.

**On `review`, don't pull the next task into the same context.** After the
frontmatter/`build.py` steps above, ask the user (three options): start a
fresh session — print the paste-ready resume line, e.g. `work T-051`, then
stop; delegate to a subagent — spawn one whose entire prompt is "read
`backlog/tasks/T-051.md` and do it," nothing re-explained, and don't let that
subagent spawn another (each level cold-starts and re-derives context, which
defeats the point); or continue here, same as today.

**The nudge.** The plugin's `UserPromptSubmit` hook watches context size from
the transcript and, once a `doing` task belongs to this session and context
has crossed `nudge_at_tokens` (`board.config.json`, default 120000), injects
a one-line reminder naming the task — re-firing every `nudge_every_tokens`
(default 25000) past that. Below the threshold, or with no `doing` task on
this session, it says nothing — no cost, no action needed. When it does fire,
treat it as "wrap up at the next stopping point": finish the current step,
then follow the continuation/incidental rules below rather than pushing on
regardless.

The threshold never applies to an `investigate` task — matching is on `status
== doing` only, and investigating one (a decision, a code dig, a websearch,
see "When a task or epic isn't ready" below) is legitimately token-heavy and
open-ended; it shouldn't get nagged to wrap up.

**Investigation → offer to delegate.** Any task, any status: before a broad
read-only dig — a multi-file grep, "where is X / what touches Y", reading a
directory to find out, a websearch — say in one line that you'll hand it to a
subagent, and spawn on a yes (`Explore` for code, general-purpose for the web).
Give it the task id and let it read `backlog/tasks/T-0xx.md`; don't re-explain.
One level only — that subagent doesn't spawn another. Fold what comes back into
the task's `## Context`, not just `## Notes`, so a cold session gets the finding
without redoing the dig.

Don't offer it when the file is already known, when you need the full text in hand
to edit next, or when the user has said not to spawn agents. And be straight about
what it buys: the subagent cold-starts uncached, so this trades higher total spend
for a longer-lived main context. Worth it when the read is wide and the answer is
small; not otherwise.

**Continuation task** — work stops mid-task, not finished. Write a new task,
`status: todo`, body opening `Continuation of T-014 — <what remains>`. Make
it a **fresh-session brief**: `## Context` carries why, the state already
reached, key files as `path:line`, the exact command to verify, and any open
question — `## Subtasks` carries what's left. A cold session should need
nothing else. Leave the original task's own `## Notes` as its history; don't
reopen it.

**Incidental finding** — an unrelated issue turns up while working something
else (e.g. a press-release JSON extraction task turns up an unrelated
extraction bug during testing). Fix it inline only if it's genuinely trivial
— a single-expression change, no new behaviour, already covered by the check
that's running — and note it in the current task's `## Notes`. Anything
touching a second file, or needing its own test, is not trivial: write it as
its own task immediately (`status: todo`, `## Context` = symptom + repro +
`path:line` evidence + `Found while working T-014.`), run `build.py`, report
it in one line, then keep going on the task actually in hand — don't fold it
into that task's scope. If it *blocks* the current task instead, set the
current task `investigate`, say why (stuck on something external, per "When a
task or epic isn't ready" above), and raise it with the user rather than
filing separately.

Either way, if no existing epic fits, create one rather than leaving the task
epic-less — every task here carries an `epic:`, and the board lays out lanes
by epic, so a blank one produces a blank lane.
