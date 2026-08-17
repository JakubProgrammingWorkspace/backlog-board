---
id: T-001
title: Migrate existing tasks/epics from project docs or by conversation with user
epic: E-001
status: todo
priority: P2
created: YYYY-MM-DD
updated: YYYY-MM-DD
---

## Context

Bootstrap task, created automatically when `backlog/` was initialized. This
project likely already tracks planned or open work somewhere ad hoc — README
TODOs, a `plan.md`, a hand-maintained open-issues doc, or only in the user's
head. This task is the single entry point for surfacing that into real
`backlog/` epics and tasks.

## Subtasks

- [ ] scan the project for task-shaped content: README, any `plan.md` /
      `TODO*` / `CHANGELOG` unreleased section, a `docs/` (or similar)
      directory for open-issue or investigation write-ups
- [ ] for anything found, propose it as a task or epic to the user —
      **never create files without an explicit yes**
- [ ] ask the user directly for open work that isn't written down anywhere
- [ ] once nothing is left to migrate, mark this epic (`E-001`) `status:
      done` and run `python3 backlog/build.py --archive`

## Notes
