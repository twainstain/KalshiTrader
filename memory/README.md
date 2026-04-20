# Second Brain (KalshiTrader)

Status + context store for the Kalshi crypto fair-value scanner project.

Scope: things that outlive a single Claude Code session but are specific to this initiative — current phase, open questions, decisions, lessons. Cross-project notes stay out; project-docs stay in `docs/`.

## Layout

- `status.md` — current state of the initiative. One short section per workstream. Update in place.
- `context.md` — durable context that shapes decisions (constraints, goals, stakeholder notes, hard lessons). Grows slowly.
- `decisions/` — one file per non-trivial decision (`YYYY-MM-DD-slug.md`). What / why / alternatives. Immutable once written.
- `notes/` — freeform working notes, research scraps. Date-prefix filenames.

## Conventions

- Markdown only.
- Date-stamp anything that can go stale: `**Updated:** YYYY-MM-DD`.
- Cross-link rather than duplicate. If content belongs in `docs/`, link to it with a one-line summary.
- Keep `status.md` short. Long content moves to `notes/` or `decisions/` and leaves a pointer.

## Relationship to other memory

- **`CLAUDE.md`** (repo root) — rules for Claude when editing this repo. Not a status log.
- **`docs/`** — published research/plans. Authoritative for strategy and execution.
- **Claude auto-memory** (`~/.claude/projects/-Users-tamir-wainstain-src-KalshiTrader/memory/`) — Claude's own recall between sessions. Not user-visible here.
- **This folder** — your second brain. User-curated, project-scoped.
