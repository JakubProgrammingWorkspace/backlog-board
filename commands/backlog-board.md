---
description: Start (or reuse) the live backlog board server and print its URL
---

Run the plugin's board server from the current project root:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/server/serve.py"
```

It auto-detects the project root by walking up from the current directory for a
`backlog/tasks/` dir (or a renamed one — see `.backlogrc.json` below). If a server for
this project is already running it reuses it (same URL, no duplicate process) — safe
to run this command repeatedly.

Report the printed `http://127.0.0.1:PORT/` URL to the user. The board is read-only:
it renders the backlog dir's `epics/*.md`, `tasks/*.md`, and `ADR.md` live (SSE
push on file change), with search and epic/priority filters. It has no write-back —
task changes still go through the normal backlog skill workflow (edit the frontmatter,
the board picks it up within ~1s).

If the command prints "no backlog/tasks/ found", the project hasn't been initialized —
see `skills/backlog/reference/init.md` first. If the project's own `backlog/` dir means
something else entirely (domain data, not this tracker), see `.backlogrc.json` in
`reference/init.md` for renaming the data dir this tool uses.
