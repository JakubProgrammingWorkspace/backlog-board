---
id: T-001
title: Define the on-disk storage format
epic: E-001
status: doing
priority: P1
created: 2026-11-02
updated: 2026-11-16
session_id:
---

## Context
Everything else reads and writes habits, so the file format has to settle first.
See ADR-001 for why this is a single JSON file.

## Subtasks
- [x] Sketch the JSON shape (habit name, created date, list of check-off dates)
- [ ] Write `load()` / `save()` with an atomic write
- [ ] Add a version field so later format changes can migrate

## Notes
2026-11-16 — shape agreed, `load()`/`save()` next.
