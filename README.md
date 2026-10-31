# pcba-triage

**Netlist-aware failure triage for PCBA bring-up, with a fault-injection benchmark.**

Given a KiCad board, a test-point map and the bring-up measurements from a first-article PCBA, a triage method ranks the most likely faulty nets and components and proposes the next probe. The proof is a benchmark. 30 known faults (6 classes × 5) are injected into a simulated Easyduino RP2040 board. Three methods get identical inputs and an identical probe budget, and each is scored on top-1/top-3 accuracy and simulated bench time:

| method | what it is |
|---|---|
| `none` | static prior, no probes: rails → clock → qspi → usb → gpio, ties broken by net degree |
| `rules` | deterministic bring-up decision tree on the panel, fixed probe order per branch |
| `agent` | LLM (OpenAI Agents SDK) with netlist tools (`get_net`, `neighbors`, `adjacent_pins`) and a budgeted `probe` tool |

## Results (committed run `results/sample`, offline: no API key)

| method | top-1 net | 95% CI (Wilson) | top-3 net | class acc | refdes acc | mean extra probes | mean bench s |
|---|---|---|---|---|---|---|---|
| none | 5/30 (17%) | 7–34% | 9/30 (30%) | 17% | 3% | 0.0 | 345 |
| rules | 29/30 (97%) | 83–99% | 29/30 (97%) | 100% | 97% | 1.9 | 384 |
| agent | not run here: needs `OPENAI_API_KEY` | | | | | | |

Rules beat the static prior on 24 cases with 0 reverse wins (exact paired p < 0.0001).

**Read this before quoting numbers:**
- **The rules baseline is near the ceiling, so this benchmark cannot show the agent winning by much.** The fixture and the rules were written by the same person, so the rules encode exactly the signatures the fixture produces (DECISIONS D12). The agent can at best tie on 29 cases and pick up the one rules miss: a GPIO15-to-GND bridge with no panel signature. One of the 29 rules hits (c11, a GPIO23/24 bridge) is a lucky low-confidence fallback guess. If the agent does not beat rules, the report says so plainly.
- **n=30 is small.** A 95% Wilson interval at 50% spans about ±17 points. Use the paired comparison in `report.md`, not the intervals, to compare methods.
- **The fixture is a behavioral model, not SPICE.** It covers the power tree, a current-limited supply, DMM R-to-GND, the crystal, the RP2040 boot chain, UART, LED and USB, with seeded ~1% noise.

Full report with the per-class table, class confusion and per-case results: [`results/sample/report.md`](results/sample/report.md).

## Quickstart

```bash
make setup                         # venv + pip + bun installs (network once); copies .env.example -> .env
make bench METHODS=none,rules      # offline, no keys: results/<run_id>/report.md + results.json
make check                         # pytest + TS typecheck + web build
# with OPENAI_API_KEY in .env (also fill PRICE_IN_PER_M / PRICE_OUT_PER_M: without them cost is n/a and MAX_USD cannot cap spend):
make bench                         # none,rules,agent; agent results cached in results/cache/agent/
make dev                           # api :8000, actors :7100, gateway :8787, web :5173
```

`make bench` reproduces everything from a clean clone. `netlist.xml`, `board.json` and `faults.json` are committed, so the benchmark never needs KiCad. Once every case is cached, the agent replays from `results/cache/agent/` with no key and no network.

## How it works

1. **Board** (`make board`). The Easyduino RP2040 is pinned to commit `53b14b66`. The netlist comes from the `.kicad_pcb` pads; `kicad-cli` is used instead when installed. The stale 2023 netlist in the source repo does not match the current PCB (DECISIONS D2). Footprint and pad positions plus the Edge.Cuts outline go into `board.json`. Net roles come from name regexes plus `roles.yaml` overrides. The power tree `VBUS → D1 → VCC → U1 → +3.3V → U2 VREG → +1V1` is derived from pin names. There is one probe point per net, placed on a real TP pad where one exists (TP1–TP14), and golden-board nominal readings for each.
2. **Faults** (`make faults SEED=2026`). Five per class, targets chosen by net role and topology, never by hard-coded refdes:
   - RAIL_SHORT: a decoupling cap on a rail
   - RAIL_OPEN: regulator or diode output open, input open, or part missing
   - WRONG_VALUE: USB series R ×100, LED series R ×10, crystal load caps ×10
   - CLOCK_FAIL: crystal missing, cracked or not starting
   - PIN_BRIDGE: adjacent QFN pins n/n+1 on the same side
   - SIGNAL_OPEN: lifted IC pin on a qspi, usb, clock or smoke-tested gpio net

   Case ids are shuffled and opaque.
3. **Protocol.** Every case starts from the standard panel: S1 R-to-GND on all rails, S2 supply current at a 150 mA limit, S3 rail voltages, S4 12 MHz at XIN/XOUT, S5 RUN level and QSPI clock activity, S7 UART banner and LED, S8 USB enumeration (`app` / `RP2 Boot` / `enum_error` / `none`). Each method may then take up to K=4 extra probes, with the budget enforced in code. Bench time per probe: v 15 s, r_gnd/r_pair 20 s, freq 45 s, logic 10 s, i_supply 10 s.
4. **Scoring.** A hypothesis hits if its nets overlap the injected fault's nets, with GND excluded and at most 3 nets and 3 refdes per hypothesis. The report also gives class accuracy, refdes accuracy, a confusion table, a paired exact test, probes, bench seconds, wall time and LLM cost.
5. **No leakage.** Triage methods receive only an `Observation` (public board + measurements) and a `ProbeSession`. `core/tests/test_no_leak.py` fails if the case id or truth fields appear in the agent prompt, or if any part of the prompt other than the measured values differs between cases.
6. **Agent path tested without a key.** `core/tests/test_agent_e2e.py` runs the real Agents SDK loop (tools, budgeted probes, strict structured output, cache) against a scripted model. Parallel probe calls in one turn are serialized, so K=4 holds (DECISIONS D13). Errored agent cases are counted in the report, not silently scored as misses (D15).

## Runtime

```
web (Vite :5173) ──► gateway (Bun :8787) ──► /api/* ──► FastAPI :8000 (fixture, methods, bench, store)
                          │ POST /socket/... grant
                          ▼
             durable-actors :7100  BoardSession (per board-case-method)  ·  BenchmarkRun (persisted cursor, resumable)
```
The dashboard shows the board in the center: parts at their real PCB positions, heat by hypothesis likelihood, TP dots and numbered probes. The S0–S8 checklist is on the left, ranked hypotheses with evidence and the next probe on the right, and the benchmark table and per-class bars at the bottom. Without the gateway it runs in mock mode from `web/public/mock/`. Supabase is optional: `store.py` writes `runs/cases/results/probes` via PostgREST when `SUPABASE_URL` and the service-role key are set (server side only); otherwise JSON only.

## Rerun on another board

1. Put the KiCad project in `boards/_src/<name>`, then run `make board BOARD=<id> PCB=<path to .kicad_pcb>`. Install `kicad-cli` to export from the schematic.
2. Write `boards/<id>/roles.yaml`: role overrides for oddly named nets, rail golden values (`nominal_v`, `r_gnd`, `load_ma`), supply limit, MCU refdes, and the smoke-test nets (UART TX, LED GPIO, LED node, banner).
3. Run `make faults BOARD=<id>`, then `make bench BOARD=<id>`. The fault picker refuses to run if a class has no candidates on the new board.
The boot-chain model is RP2040-specific (XIP → BOOTSEL ROM). Another MCU needs its own boot rules in `fixture.simulate()`.

## Limitations

- Behavioral fixture. Signatures are cleaner than a real bench: no intermittents, no multiple simultaneous faults, no partial (10–200 Ω) shorts, and no thermal or ESD-damage progression.
- Rails share one golden R-to-GND each, so the panel cannot localize which decoupling cap is shorted. Refdes accuracy for RAIL_SHORT is therefore partly a guess, and real benches use thermal imaging or milliohm mapping for this.
- PIN_BRIDGE on unexercised GPIOs has no panel signature; only probes can find it, and K=4 is a small budget.
- Rules and fixture share an author (above), and n=30.

## Next steps

Production test coverage beyond bring-up is out of scope: ICT and flying probe, boundary scan on SWD, FCT, burn-in, thermal and EMC pre-scan. Within scope, the next steps are:
- A harder fault set: soft shorts, intermittent opens and two-fault cases.
- Rules written blind to the fixture.
- Real UART output from Renode (stretch E).
- A second board (Easyduino UNO, Cicada-HW).

## Demo script (3 min)

1. `make dev`, open :5173. The mode badge shows LIVE (with only the static build or no gateway it shows MOCK).
2. Pick case `c09`, run the panel. S5 shows QSPI_SCLK floating and S8 shows `RP2 Boot`.
3. Triage with `rules`. The top hypothesis is SIGNAL_OPEN on `/QSPI_SCLK`, with probes 1–4 on the QSPI test points. Tick "reveal injected fault".
4. Pick `c20`. The LED blinks dim, and rules name R14 as a WRONG_VALUE in one look, with no probes.
5. Pick `c16` (GPIO15 bridged to GND). The panel is all green, and rules spend 4 probes and miss. This is the case where netlist tools (`adjacent_pins`) should matter.
6. Bottom panel: benchmark table, per-class bars, verdict line. Click "Run benchmark (live)" to watch the BenchmarkRun actor step through cases.
