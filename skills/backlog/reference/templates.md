# Templates

## Epic

```markdown
---
id: E-002
title: Extraction hardening
created: 2026-08-11
updated: 2026-08-11
---

## Goal
One or two sentences: what's true when this epic is done.

## Scope / Non-goals
What's explicitly in and out.
```

`status` is not set by hand while the epic has tasks — `build.py` derives it from them:
all `done` → `done`; any `doing` → `doing`; else any `investigate` → `investigate`;
else every task `done` or `review` (at least one `review`) → `review`; otherwise
`todo`. A task-less epic (nothing broken out yet) can carry an explicit `status:` on
its own frontmatter instead — including `investigate`, e.g. for an epic that's just
an idea nobody's scoped yet.

## Task

```markdown
---
id: T-014
title: DXC prior_guidance never populated from prose
epic: E-002
status: todo          # investigate | todo | doing | review | done
priority: P1           # P1 = urgent, blocking other work or a user-facing break
                        # P2 = normal — the default for planned work
                        # P3 = low / someday — nice to have, no pressure
created: 2026-08-11
updated: 2026-08-11
completed:              # set to today's date alongside status: done — by a human
                         # accepting a review task, or by the board server's
                         # auto-accept sweep — drives the retention sweep, see SKILL.md
session_id:              # set to $CLAUDE_CODE_SESSION_ID alongside status: doing —
                         # which session is (or was last) working this task
docs: docs/pipeline/extraction-open-concerns.md#a-dxc
---

## Context
Symptom today, why it matters, what's already known.

## Subtasks
- [ ] first concrete step
- [ ] second

## Notes
Findings appended as work proceeds. Keep this, don't overwrite — it's the
task's history.
```

`docs:` is optional — a pointer to a source investigation living in the
project's normal docs folder. The board renders it as a link. Omit if there
isn't one.

Statuses:
- `investigate` — not yet actionable: scope depends on a decision only the user can
  make, an investigation, a websearch, or something concrete and *external* is
  stopping otherwise-understood work (another task, a missing credential, a vendor
  fix).
- `todo` — understood and ready to pick up, just not started.
- `doing` — actively being worked.
- `review` — the agent believes the work is finished; awaiting human acceptance.
  Set this, not `done`, when work wraps up (see SKILL.md's "Finishing a task").
  Auto-promotes to `done` after a configurable idle window unless a human accepts
  (or rejects) it first.
- `done` — a human has looked at the work and accepted it.

## Continuation task

Work on a task stopped before it finished — filed per "Session boundaries" in
SKILL.md so a fresh session (or a subagent) can pick it up with no other
reading. Same frontmatter shape as any task; the body is what makes it a
continuation:

```markdown
---
id: T-052
title: Finish backfilling prior_yoy_* for FY24 filings
epic: E-006
status: todo
priority: P2
created: 2026-08-15
updated: 2026-08-15
---

## Context
Continuation of T-041 — backfill stalled partway through.

Ran `press_release_compute_growth` over FY24 filings for the SEC-derived-metrics
backlog (E-006). 340/512 filings processed before the session ended; the
remaining 172 are tickers `sec_cik >= 0001600000` (alphabetically the back half
of the queue in `services/derived_metrics/press_release_compute_growth.py`).
No code changes needed — this is a data-backfill run, not a bugfix.

Verify: `make uv run manage.py backfill_growth_metrics --dry-run` should report
0 filings with `prior_yoy_revenue` still null once done.

Open question: none — straightforward continuation of the same command.

## Subtasks
- [ ] Run the backfill command for the remaining tickers
- [ ] Confirm the dry-run check above reports 0 nulls
- [ ] Spot-check 3 tickers against the source filing

## Notes
```

## Incidental finding

An unrelated issue turned up while working a different task — written
immediately, without waiting for a yes (see the standing exception in
SKILL.md's Rules), so it isn't lost if the session ends before it's raised in
chat:

```markdown
---
id: T-053
title: Column-drift mislabels 3-column YoY tables as flat growth
epic: E-003
status: todo
priority: P2
created: 2026-08-15
updated: 2026-08-15
---

## Context
Found while working T-041 (press-release JSON extraction backfill).

`ColumnIdentifier` (services/derived_metrics/press_release_compute_growth.py:118)
picks the first two numeric columns as (current, prior) — a 3-column table with
a variance column in the middle gets its 3rd column read as `prior`, producing
a fake 0% growth row. Repro: HPE Q3 FY25 filing, `backlog_growth` for
"Services" — expected ~8% YoY, extracted 0%.

## Subtasks
- [ ] Detect a variance/percent-labeled middle column and skip it in the
      (current, prior) pick
- [ ] Re-run extraction for tickers already affected, confirm the fix

## Notes
```

If it's genuinely trivial (one-expression fix, no new behaviour, already
covered by the test in hand), fix it inline instead and note it in the
*current* task's `## Notes` — no new file for that case.

## ADR entry

Appended to `backlog/ADR.md`, newest last:

```markdown
## ADR-003 — Regex extraction over LLM extraction
Date: 2026-08-11 · Status: accepted · Supersedes: —

**Context**
What prompted the decision.

**Decision**
What was chosen.

**Rejected**
The alternative(s), and the specific reason each was rejected — this is the
part a future reader needs, so they don't re-propose it.

**Consequences**
What this changes going forward (naming, follow-up cleanup, constraints).
```

Number sequentially. `Status` is `proposed`, `accepted`, or `superseded`
(add `Superseded-by: ADR-00x` when it is). Never split into per-file ADRs
until `ADR.md` passes ~500 lines.
