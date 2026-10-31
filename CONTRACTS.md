# CONTRACTS (frozen at T+0:20 — lead-only edits)

Source of truth in code: `core/triage/schemas.py` (pydantic) and `shared/types.ts`. `actors/src/types.ts` mirrors the subset the actors use (the durable-actors generator reads types from its own project only). If these disagree, `schemas.py` wins and the lead fixes the others.

## Board (public)
```
Board        {board_id, components:[Component], nets:[Net], power_tree:[PowerEdge], nominal:{<net>: Nominal}, testpoints:[TestPoint], outline:{w,h}}
Component    {ref, value, footprint, x, y, rot, dnp, pins:[{pin, name, net, x?, y?}]}     # mm, outline origin, y down; pin x/y = pad centre
Net          {name, role, nodes:[{ref, pin}]}       role ∈ power_rail|ground|clock|qspi|usb|gpio|reset|other
PowerEdge    {ref, in_net, out_net, nominal_v}
Nominal      {v?, r_gnd?, r_gnd_min?, freq_hz?}     # r_gnd = golden-board unpowered R-to-GND (null = OL)
TestPoint    {tp, net, ref, pin, kinds:[MeasKind]}  # tp ids: TP_<net>, plus SUPPLY (i_supply) and USB_HOST (usb_enum)
```

## Measurements and cases
```
MeasKind     v | r_gnd | r_pair | freq | i_supply | logic | uart | usb_enum
Measurement  {tp, tp2?, kind, value: number|string, unit, stage}     stage ∈ S0..S8 | PROBE
             r_gnd/r_pair: ohm, 1e9 = open (OL) · v: V · freq: Hz (0 = no oscillation) · i_supply: mA
             logic: toggling | toggling_dim | high | low | floating   (toggling_dim = LED blinks but visibly dim)
             uart: banner text, "" = silent · usb_enum: app | RP2 Boot | enum_error | none
FaultCase    (HIDDEN) {case_id, cls, target:{nets, refs, pins:["REF.PIN"]}, params, seed}
             cls ∈ RAIL_SHORT | RAIL_OPEN | WRONG_VALUE | CLOCK_FAIL | PIN_BRIDGE | SIGNAL_OPEN
             case_id is opaque (c01..c30, shuffled) — it never encodes the class
Observation  {board_public, measurements:[Measurement], probe_budget}  # the ONLY input a triage method gets (+ a ProbeSession)
ProbeRequest {tp, kind, tp2?}
Hypothesis   {rank, cls, nets:[≤3], refs:[≤3], confidence 0..1, evidence:[str], next_probe?: ProbeRequest}
TriageResult {case_id, method, hypotheses:[≤5], probes:[Measurement], probes_used, bench_seconds, wall_ms, cost_usd|null, notes:[str]}
```

## Protocol (identical for every method)
- Standard panel: S1 r_gnd on every rail · S2 i_supply · S3 v on every rail · S4 freq on the MCU XIN/XOUT nets · S5 v on reset net + logic on QSPI clock · S7 uart + logic on LED GPIO and LED node · S8 usb_enum. S0/S6 informational, not simulated.
- Up to K=4 extra probes through `ProbeSession.probe()`, budget enforced in code. Invalid probes raise and do not count.
- Bench time per probe (s): v 15 · r_gnd/r_pair 20 · freq 45 · logic 10 · i_supply 10 · uart 30 · usb_enum 30. `bench_seconds` = panel time + probe time.
- Scoring: a hypothesis hits if `nets ∩ truth.nets ≠ ∅` (GND excluded on both sides). top-k = any hit in the first k. Class accuracy = top-1 class. Refdes accuracy = top-1 refs ∩ truth refs.

## Python API (FastAPI :8000)
```
GET  /health
GET  /board/{board_id}                    -> Board
GET  /cases/{board_id}                    -> [{case_id}]               (public ids only)
POST /session/{case_id}/panel             -> {case_id, measurements, probe_budget, panel_seconds}   (fresh session)
POST /session/{case_id}/probe  {tp,kind,tp2?} -> {measurement, probes_left, bench_seconds}   409 budget, 400 bad probe
POST /triage/{case_id}         {method}   -> TriageResult              (fresh session, same protocol as the bench)
POST /bench                    {methods, case_ids?} -> {run_id, methods}
GET  /runs                                -> [run_id]
GET  /runs/{run_id|latest}                -> BenchResults (includes truth: post-run only)
```

## Actors (durable-actors 0.14.6, dev on :7100)
- `BoardSession` actorId `${board}-${case}-${method}`. Persisted: stage, status, measurements, probes, hypotheses, error. Methods: `start()`, `probe(req)`, `triage()`, `getState()` (all `@Interleave` except getState). Socket in: `{type:"start"} | {type:"probe", probe} | {type:"triage"}`. Socket out: `{type:"session", state: SessionState}` after every change (`state`/`state_update` are reserved by durable-actors).
- `BenchmarkRun` actorId = run_id. Persisted: status, queue, cursor, results, error. `begin(board_id, methods, case_ids|null)` queues; `resume()` (`@Interleave`) runs sequentially from the persisted cursor, saving it after each case; `pause()`. Resume on `onConnect` or `{type:"resume"}` when status is running. No timers or alarms are used. Socket out: `{type:"bench", state: BenchRunState}`.

## Gateway (Bun :8787)
```
GET  /health                    -> {ok, api, actors}
*    /api/*                     -> proxied to the Python API
POST /socket/{BoardSession|BenchmarkRun}/{id} -> {websocketUrl, homeRegion, connectByMs}
POST /bench/{run_id}/begin  {board_id?, methods?, case_ids?} -> BenchRunState, then resume() in the background
*    other paths                -> web/dist (built dashboard)
```

## Files
`results/<run_id>/results.json` = `BenchResults` in `shared/types.ts` (methods, comparisons, cases, results, faults, panels). `results/<run_id>/report.md` is rendered from it.
