# Lane C — Runtime & Data (Codex)
Owned: `core/triage/{api,store}.py`, `actors/`, `gateway/`, `supabase/`, `core/tests/test_api.py`.
Goal: FastAPI per CONTRACTS.md; store.py (JSON always, Supabase via PostgREST when keys set); durable-actors `BoardSession` + `BenchmarkRun` (persisted cursor, resume on connect/"resume", no timers); Bun gateway :8787 (api proxy, websocket grants, bench begin, static web).
Read first: `actors/node_modules/durable-actors/README.md` and TerseAI/durable-actors `docs/reference/typescript-guide.md`.
Gate: `make actors` (Ready on :7100) then `cd gateway && bun run generate && bunx tsc --noEmit`, `curl :8787/health` -> api+actors true.
