---
id: E-001
title: Migrate existing planning docs into backlog/
created: YYYY-MM-DD
updated: YYYY-MM-DD
---

## Goal

Every task, epic, or decision currently sitting in ad-hoc files — a stale
`plan.md`, a hand-maintained open-issues doc, README TODOs, an old ADR file
— has either become a real `backlog/` task/epic, or was explicitly decided
not to be one. Nothing silently lost, nothing migrated without a yes.

## Scope / Non-goals

In scope: finding candidate planning docs in this project, proposing which
items look like real tasks/epics/decisions, migrating on explicit
confirmation.

Not in scope: rewriting the source docs' prose, deleting the source docs.
Leave them in place (or ask before removing) — a task's `docs:` frontmatter
field can point back at the original instead of duplicating it.
