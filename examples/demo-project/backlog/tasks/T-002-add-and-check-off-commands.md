---
id: T-002
title: Implement `habit add` and `habit done`
epic: E-001
status: investigate
priority: P1
created: 2026-11-02
updated: 2026-11-16
---

## Context
Blocked by T-001: the commands need `load()` / `save()`. Stays `investigate` until
T-001 is done, then moves to `todo`.

## Subtasks
- [ ] `habit add <name>` creates a habit
- [ ] `habit done <name>` records today's date, idempotent for the same day
- [ ] Friendly error for an unknown habit name

## Notes
