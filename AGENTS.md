# AGENTS.md (read by Codex)

1. Read `CONTRACTS.md`, then your lane file `lanes/<X>.md`.
2. Stay inside your lane's owned paths. Never edit `CONTRACTS.md`, `core/triage/schemas.py` or `shared/types.ts` (lead only).
3. Never invent an API: read the installed package's README/types first. If you cannot verify something, build a fallback and add a line to `DECISIONS.md` under your lane.
4. Gate: post `DONE <lane> <test cmd> <output tail>`. Stuck 10 min: post `BLOCKED <what> <tried>`.
5. `make check` must stay green: pytest + TS typecheck + web build.
