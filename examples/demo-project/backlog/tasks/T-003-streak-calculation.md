---
id: T-003
title: Streak calculation
epic: E-001
status: todo
priority: P2
created: 2026-11-03
updated: 2026-11-10
---

## Context
Pure function over a list of dates, so it does not depend on the storage format
and can be worked on in parallel with T-001.

## Subtasks
- [ ] `current_streak(dates, today)` counts consecutive days ending today or yesterday
- [ ] `longest_streak(dates)`
- [ ] Unit tests for gaps, duplicates and an empty list

## Notes
