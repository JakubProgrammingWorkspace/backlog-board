---
description: Write a continuation task for the current in-flight work and print the resume line
---

Use when the user asks to "stop here", "hand off", or "wrap up this session" while a
task is still `doing` — not for a task that's actually finished (that's the normal
`review` flow in the `backlog` skill, see "Session boundaries" in its `SKILL.md`).

1. Find the task on this session: the `doing` row in `backlog/INDEX.md` whose
   `session_id` matches `$CLAUDE_CODE_SESSION_ID`. If none, say so and stop — there's
   nothing to hand off.
2. Write a new task per the "Continuation task" template in
   `skills/backlog/reference/templates.md`: `status: todo`, body opening
   `Continuation of T-0xx — <what remains>`, and a **fresh-session brief** in
   `## Context` — why, the state already reached, key files as `path:line`, the exact
   command to verify, any open question — plus `## Subtasks` for what's left. Enough
   that a cold session needs nothing else.
3. Run `python3 backlog/build.py`.
4. Report the new task's id and title, then print the paste-ready resume line:
   `work T-0xx`. Don't touch the original task's status — it stays `doing` until
   someone actually finishes or reopens it; this command only forks off what's left.
