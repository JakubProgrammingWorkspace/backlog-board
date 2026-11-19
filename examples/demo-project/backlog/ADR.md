# Architecture decisions

## ADR-001 — Single JSON file over SQLite
Date: 2026-11-02 · Status: accepted · Supersedes: —

**Context**
The tool stores a few dozen habits with a date per check-off. It runs locally, one
user, one process.

**Decision**
Store everything in one JSON file under the user's config directory.

**Rejected**
SQLite: a real database for data this small adds a schema and migrations for no
benefit. Revisit if the file grows past a few MB.

**Consequences**
Writes must be atomic (write to a temp file, then rename). The format carries a
version field so it can migrate.
