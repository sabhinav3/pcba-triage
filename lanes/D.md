# Lane D — Dashboard (Claude)
Owned: `web/`.
Goal: board in the center (SVG outline, parts at PCB positions, heat = hypothesis likelihood, TP dots, numbered probe markers); left S0–S8 checklist; right ranked hypotheses + evidence + next probe + probe log; bottom benchmark table + per-class bars. Mock mode from `web/public/mock/{board,results}.json` until the gateway is live.
Gate: `cd web && bun run build` (tsc + vite) and a screenshot of mock + live modes.
