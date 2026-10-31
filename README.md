# pcba-triage

**Netlist-aware failure triage for PCBA bring-up, with a fault-injection benchmark.**

When the first prototype boards come back from assembly, some of them don't work, and an engineer spends hours with a multimeter working out why. pcba-triage reads the board's own KiCad netlist and takes the standard bring-up measurements. It then ranks the most likely faulty nets and components and proposes the next test point to probe, within a fixed probe budget.

The claim is backed by a benchmark. 30 known faults (6 classes × 5) are injected into a simulated [Easyduino RP2040](https://github.com/Hanqaqa/Easyduino) board. Three triage methods get identical inputs and an identical budget of 4 extra probes. Each is scored on whether it names the broken net first (top-1), within its first three guesses (top-3), and how many simulated bench-seconds it spends.

![Dashboard on case c09: rules triage names an open QSPI clock pin on U2, with probes 1–4 on the QSPI test points](docs/dashboard_c09.png)

## Contents

- [What it does](#what-it-does)
- [Results](#results)
- [Where it is useful](#where-it-is-useful)
- [How it differs from existing tools](#how-it-differs-from-existing-tools)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Make targets](#make-targets)
- [HTTP API](#http-api)
- [Live runtime: Durable Actors](#live-runtime-durable-actors)
- [Repository layout](#repository-layout)
- [Tests](#tests)
- [Run it on another board](#run-it-on-another-board)
- [Limitations](#limitations)
- [Next steps](#next-steps)
- [Demo script](#demo-script-3-minutes)
- [Credits and licenses](#credits-and-licenses)

## What it does

| Input | Output |
|---|---|
| A KiCad board: netlist, footprint positions, board outline | A public board model: parts at real positions, net roles, power tree, test points, golden readings |
| Bring-up measurements from stages S1–S8 (resistance to ground, supply current, rail voltages, clock, boot, firmware smoke test, USB) | Up to 5 ranked hypotheses: fault class, nets, parts, confidence, evidence, next probe |
| A budget of 4 extra probes | The probes actually taken, simulated bench-seconds, wall time, LLM cost |

Three methods compete on the same inputs:

| Method | What it is |
|---|---|
| `none` | Static prior, no probes: rails → clock → QSPI flash → USB → GPIO, ties broken by how many parts sit on the net |
| `rules` | A deterministic bring-up decision tree on the panel, with a fixed probe order per branch |
| `agent` | An LLM ([OpenAI Agents SDK](https://github.com/openai/openai-agents-python)) with netlist tools (`get_net`, `neighbors`, `adjacent_pins`) and a budgeted `probe` tool |

## Results

Committed run [`results/sample`](results/sample/report.md), fault seed 2026, run offline with no API key, so the agent row is empty. To fill it, add an OpenAI key and run `make bench RUN_ID=sample` ([Run the LLM agent](#run-the-llm-agent)).

| method | top-1 net | 95% CI (Wilson) | top-3 net | class acc | refdes acc | mean extra probes | mean bench s |
|---|---|---|---|---|---|---|---|
| none | 5/30 (17%) | 7–34% | 9/30 (30%) | 17% | 3% | 0.0 | 345 |
| rules | 29/30 (97%) | 83–99% | 29/30 (97%) | 100% | 97% | 1.9 | 384 |
| agent | not run: needs `OPENAI_API_KEY` | | | | | | |

Rules beat the static prior on 24 cases with 0 reverse wins (exact paired p < 0.0001).

Per class (top-1 net hits out of 5):

| class | none | rules |
|---|---|---|
| RAIL_SHORT | 2 | 5 |
| RAIL_OPEN | 3 | 5 |
| WRONG_VALUE | 0 | 5 |
| CLOCK_FAIL | 0 | 5 |
| PIN_BRIDGE | 0 | 4 |
| SIGNAL_OPEN | 0 | 5 |

**Read this before quoting numbers.**

- **The rules baseline is near its ceiling.** The fixture and the rules were written by the same author, so the rules encode exactly the signatures the fixture produces ([DECISIONS D12](DECISIONS.md)). At most, the agent can tie on 29 cases and pick up the one rules miss: case c16, a GPIO15-to-GND solder bridge with no panel signature. One of the 29 rules hits (c11, a GPIO23/24 bridge) is a lucky fallback guess at confidence 0.05. If the agent does not beat rules, the report says so plainly.
- **n=30 is small.** A 95% Wilson interval at 50% spans about ±17 points. Compare methods with the paired test in `report.md`, not by eyeballing the intervals.
- **The fixture is a behavioural model, not SPICE** ([Limitations](#limitations)).

The full report has the class-confusion tables and every per-case result: [`results/sample/report.md`](results/sample/report.md).

## Where it is useful

The target moment is first-article bring-up: the days after prototype boards return from assembly, before any production test fixture exists.

- **Hardware startups without a test engineer**: 10 boards back, 3 won't boot, and the KiCad files are all they have.
- **Contract-manufacturer NPI teams**: consistent first-run triage that does not depend on which technician is on shift.
- **Validation labs** bringing up many board revisions: one method that re-targets to each new netlist.
- **Teams evaluating AI debugging assistants**: drop in a board, inject faults, and score the agent against a decision tree before trusting it on real hardware.
- **Students and makers**: guided debug of a first custom board in the right bring-up order.

## How it differs from existing tools

| Approach | Where it stops | What pcba-triage adds |
|---|---|---|
| In-circuit test (bed-of-nails) | Needs a custom fixture per board, which does not exist at first article | Works on board one with a multimeter, a scope and the netlist |
| Flying probe | A production machine; reports pass/fail per test, not a ranked diagnosis | Ranks causes and chooses the next probe from a handful of readings |
| Boundary scan (JTAG) | Only nets on JTAG-capable chips; no power tree, clocks or analog. The RP2040 debugs over SWD, not JTAG boundary scan | Covers power tree, clock, boot chain, USB and LEDs |
| Bench automation (Python, SCPI, sequencers) | Says what failed, not why | Turns failures into ranked hypotheses with evidence |
| Pasting symptoms into a chatbot | No netlist, no probe budget, accuracy unknown | Netlist tools, a hard budget and a measured score |

This does not replace production test. It covers the step before production test exists.

## Architecture

```
                 ┌────────────────┐     ┌────────────────┐     ┌────────────────────┐
  browser ─────► │ Web dashboard  │────►│ Gateway        │────►│ Durable Actors     │
                 │ Vite + TS :5173│     │ Bun + TS :8787 │     │ (Terse) :7100      │
                 └────────────────┘     └───────┬────────┘     │ BoardSession       │
                                                │ /api/*       │ BenchmarkRun       │
  make bench ──────────────┐                    ▼              └─────────┬──────────┘
  (offline CLI)            │            ┌────────────────┐               │ fetch
                           │            │ FastAPI :8000  │◄──────────────┘
                           ▼            └───────┬────────┘
  ┌──────────────────────────────────────────────────────────────────────────────────┐
  │ Python core                                                                        │
  │  board model ──► fault injector ──► fixture (simulated board, 4-probe budget)      │
  │                                       │                                           │
  │                                       ▼                                           │
  │                          methods: none | rules | agent ──► scoring ──► report.md  │
  └───────▲───────────────────────────────────────────┬───────────────────┬───────────┘
          │                                           │                   │
   KiCad files (Easyduino RP2040)            OpenAI API (optional)   Supabase (optional)
                                                                     or results/*.json
```

`make bench` needs only the Python core. The dashboard, gateway and actors are the live demo layer.

## Tech stack

| Layer | Technology (version) | What it does here | Why |
|---|---|---|---|
| Board ingest | Python; custom `.kicad_pcb` parser; PyYAML 6.0 | Pads become `netlist.xml`; positions, outline, roles, power tree, test points and golden readings become `board.json` | `kicad-cli` was not available, and the PCB is newer than the repo's exported netlist ([D2](DECISIONS.md)) |
| Contracts | pydantic 2.14; TypeScript 5.9 mirror in `shared/types.ts` | One schema for Board, Measurement, FaultCase, Observation, Hypothesis, TriageResult | Python and TypeScript check against the same shapes |
| Fault injector | Python, seeded | 30 faults, 6 classes × 5, picked by net role and topology | Reproducible; never hard-coded to refdes |
| Fixture | Python behavioural model | Power tree, current-limited supply, DMM R-to-GND, crystal, RP2040 boot chain, UART, LED, USB, ~1% seeded noise; `ProbeSession` enforces the budget under a lock | Scoring needs known faults; SPICE would not fit the time box |
| Methods | Python; OpenAI Agents SDK `openai-agents` 0.24.0 (`openai` 3.42) | Static prior, decision tree, LLM agent with function tools and strict structured output | Same inputs and budget for all three |
| Scoring and report | Python | Top-1/top-3, Wilson 95% CI, exact paired binomial test, per-class and confusion tables | n=30 needs honest error bars |
| API | FastAPI 0.162, Uvicorn 0.54 | Board, cases, panel, probe, triage, bench and runs over HTTP | Same code path as the CLI benchmark |
| Storage | Supabase PostgREST via httpx 0.28; JSON fallback | Tables `runs`, `cases`, `results`, `probes` | Optional; a missing key never crashes anything |
| Actors | Terse Durable Actors 0.14.6 on Bun ≥ 1.4.2 | `BoardSession` and `BenchmarkRun` with persisted, resumable state | Survives restarts; resumes a benchmark from its cursor |
| Gateway | Bun + TypeScript, generated actor client | Proxies `/api/*`, grants actor WebSockets, serves the built dashboard | One origin for the browser |
| Dashboard | Vite 7, TypeScript, hand-written SVG, no UI framework | Board at real PCB positions with fault heat, S0–S8 checklist, hypotheses, probe log, benchmark table; mock mode | Small, fast, little to break on stage |
| Quality gate | pytest 9 (31 tests), `tsc --noEmit` × 3, Vite build | `make check` | One command before any merge |

## How it works

### 1. Board model (`make board`)

The Easyduino RP2040 source is pinned to commit `53b14b66` (`EASYDUINO_REV` in the Makefile). `core/triage/pcb_netlist.py` reads every pad's net and pin function, plus the DNP flags, from the `.kicad_pcb` and writes a KiCad-XML-style `netlist.xml`. When `kicad-cli` is installed, `make board` exports from the schematic instead. `core/triage/netlist.py` and `pcb_pos.py` then build `board.json`:

- **Components** with value, footprint, position, rotation, DNP flag and every pin's name, net and pad centre.
- **Net roles** (`power_rail`, `ground`, `clock`, `qspi`, `usb`, `gpio`, `reset`, `other`) from name regexes plus `roles.yaml` overrides.
- **Power tree**, derived from pin names: `VBUS → D1 → VCC → U1 → +3.3V → U2 VREG → +1V1`.
- **Test points**: one per net with 2+ nodes, placed on the most probe-friendly pad. Real TP pads (TP1–TP14) come first, then 0402 passives, connectors, small parts, and the fine-pitch IC last. Two pseudo test points: `SUPPLY` (bench supply current) and `USB_HOST` (host enumeration log).
- **Golden readings** per net, produced by the same fixture with no fault.
- **Outline** from Edge.Cuts, so the dashboard draws the real 21 × 51 mm board.

`netlist.xml`, `board.json` and `faults.json` are committed, so the benchmark never needs KiCad.

### 2. Bring-up stages

| Stage | What is measured | Simulated |
|---|---|---|
| S0 | Visual/AOI + BOM check | informational |
| S1 | Unpowered resistance to GND on every rail | yes |
| S2 | Current-limited power-up (5 V, 150 mA limit): supply current | yes |
| S3 | Rail voltages: VBUS, VCC, +3.3V, +1V1 (RP2040 internal regulator) | yes |
| S4 | 12 MHz crystal frequency at XIN/XOUT | yes |
| S5 | RUN level; QSPI clock activity (boot from flash vs USB BOOTSEL ROM) | yes |
| S6 | SWD / flash ID | informational |
| S7 | Firmware smoke test: UART banner `BOOT OK fw=1.0`, LED GPIO toggle | yes |
| S8 | USB enumeration: `app`, `RP2 Boot`, `enum_error` or `none` | yes |

### 3. Fault model (`make faults SEED=2026`)

Five cases per class. Targets are chosen from the netlist by net role and topology, never by hard-coded refdes. Case ids (`c01`–`c30`) are shuffled and opaque.

| Class | What is injected | Panel signature |
|---|---|---|
| RAIL_SHORT | A decoupling cap on a rail shorted to GND | R-to-GND 0.1–2 Ω, supply at the current limit, rail ≈ 0 V, everything downstream dead |
| RAIL_OPEN | Regulator or diode output open, input open, or part missing | Rail 0 V with normal R-to-GND, low supply current, downstream dead |
| WRONG_VALUE | USB series R ×100 (27 Ω → 2.7 kΩ), LED series R ×10, crystal load caps ×10 | A parameter shift on that net only: enumeration error, dim LED, clock pulled |
| CLOCK_FAIL | Crystal missing, cracked or not starting | Rails fine, 0 Hz on XIN/XOUT, no banner, no USB of any kind |
| PIN_BRIDGE | Solder bridge between adjacent QFN pins n and n+1 on the same side | R-pair ≈ 0 Ω; a QSPI pair boots to `RP2 Boot`; two GPIOs follow each other; on an unexercised GPIO, no panel signature at all |
| SIGNAL_OPEN | Lifted IC pin on a QSPI, USB, clock or smoke-tested GPIO net | QSPI → `RP2 Boot`, no banner; USB D± → banner but no enumeration; LED GPIO → LED test fails |

The fixture adds seeded Gaussian noise of about 1% to analog readings (5 ppm on frequency).

### 4. Measurement protocol

Every method sees the same standard panel (S1–S5, S7, S8), then may take **up to K=4 extra probes** through `ProbeSession.probe()`. The budget is enforced in code: invalid probes raise and do not count. A lock holds the budget even when the agent fires several probes in parallel ([D13](DECISIONS.md)).

| Probe kind | Bench cost |
|---|---|
| `v` (voltage) | 15 s |
| `r_gnd`, `r_pair` (resistance) | 20 s |
| `freq` | 45 s |
| `logic` | 10 s |
| `i_supply` | 10 s |
| `uart`, `usb_enum` | 30 s |

`bench_seconds` = panel time + probe time, so a method that probes everything pays for it.

### 5. Triage methods

**`none`** ranks nets by a static prior and takes no probes.

**`rules`** is a fixed decision tree, in order:

1. Hard short on a rail.
2. An over-voltage rail, i.e. a power-to-power bridge.
3. The first dead rail whose parent is alive, i.e. an open regulator path.
4. A dead or off-frequency clock.
5. The boot chain: QSPI floating, or `RP2 Boot`.
6. The LED.
7. The UART.
8. USB.
9. If the whole panel passes, it spends its probes on GPIO nets in a fixed order.

**`agent`** is an OpenAI Agents SDK `Agent` with strict structured output (`AgentAnswer`) and four function tools:

| Tool | Returns |
|---|---|
| `get_net(name)` | Nodes (`REF.PIN(pin name)`), role, test point, golden readings |
| `neighbors(ref_or_net)` | A part's nets, or a net's parts plus nets one small part away |
| `adjacent_pins(ref, net)` | Physically adjacent pins (n, n+1 on the same side) of a fine-pitch IC, with their nets and test points |
| `probe(tp, kind, tp2)` | One measurement against the budget; an error string once the budget is spent |

The prompt is in [`prompts/triage_agent.md`](prompts/triage_agent.md); the default model is `gpt-5-mini` (`OPENAI_MODEL`), with at most 16 turns. Answers are sanitized: nets and refs not in the netlist are dropped, GND is removed, confidence is clamped to 0–1, and there are at most 5 hypotheses with at most 3 nets and 3 refs each.

### 6. Scoring

A hypothesis **hits** if its nets overlap the injected fault's nets (GND excluded on both sides). Top-k = any hit in the first k. The report also gives class accuracy (top-1 class), refdes accuracy (top-1 refs ∩ truth refs), a class-confusion table, Wilson 95% intervals and an exact two-sided paired test on discordant cases. Methods that crash on a case score a miss, and the report counts those errors separately ([D15](DECISIONS.md)).

### 7. No leakage

Triage methods receive only an `Observation` (public board + measurements) and a `ProbeSession`. `core/tests/test_no_leak.py` fails if the case id or any truth field appears in the agent prompt, or if any part of the prompt other than the measured values differs between cases. `core/tests/test_agent_e2e.py` also checks the bytes the model actually received.

### 8. Reproducibility

- All randomness is seeded: faults (`SEED`) and per-measurement noise.
- Agent answers are cached in `results/cache/agent/` by `sha256(case | method | model | prompt version | prompt file)`. With every case cached, `make bench` replays the agent with no key and no network. With a partial cache and no key, the agent is skipped entirely, so methods are always compared on identical case sets.
- `MAX_USD` (default 15) stops a live agent run once spend passes the cap, provided `PRICE_IN_PER_M` and `PRICE_OUT_PER_M` are set.

## Quickstart

### Prerequisites

| Tool | Version | macOS | Linux |
|---|---|---|---|
| Python | 3.10+ | `brew install python@3.12` | distro package or pyenv |
| Bun | ≥ 1.4.2 | `brew install oven-sh/bun/bun` or `curl -fsSL https://bun.sh/install \| bash` | same script |
| git, make | any | `xcode-select --install` | `build-essential`, `git` |

The Durable Actors runtime ships prebuilt for macOS and Linux on arm64 and x64. On Windows, use WSL 2. macOS's built-in `python3` is often 3.9; pass `PY_SYS=python3.12` to `make setup` in that case.

### Run the benchmark (offline, no keys)

```bash
make setup                         # venv + pip + bun installs (network once); copies .env.example -> .env
make bench METHODS=none,rules      # writes results/<run_id>/report.md + results.json
make check                         # pytest + TypeScript typecheck + web build
```

### Run the LLM agent

Edit `.env` and set `OPENAI_API_KEY`. Also set `PRICE_IN_PER_M` and `PRICE_OUT_PER_M` from OpenAI's pricing page for your `OPENAI_MODEL`; without them, cost shows as n/a and `MAX_USD` cannot cap spend. Then run:

```bash
make bench                         # none, rules and agent
make bench RUN_ID=sample           # same, and refreshes the committed sample report
```

Each agent case logs one line. Cached cases cost nothing on rerun.

### Live dashboard

```bash
make dev                           # api :8000, actors :7100, gateway :8787, web :5173 (Ctrl-C stops all)
```

Open http://localhost:5173. The badge shows **LIVE** when the gateway answers, otherwise **MOCK**, which replays `web/public/mock/`. The first actor start downloads the Durable Actors runtime for your platform once.

### Supabase (optional)

1. Create a Supabase project and run [`supabase/migrations/001_init.sql`](supabase/migrations/001_init.sql) in its SQL editor.
2. Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`. The key is used server-side only, by `core/triage/store.py`, and never reaches the browser bundle.
3. Run `make bench` again. The `[store]` log line confirms the write; without keys, results stay as JSON in `results/<run_id>/`.

## Configuration

`.env` (copied from `.env.example` by `make setup`, gitignored):

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | empty | Enables the agent method; empty means it is skipped with a log line |
| `OPENAI_MODEL` | `gpt-5-mini` | Model for the agent; part of the cache key |
| `MAX_USD` | `15` | Stop the agent run past this spend |
| `PRICE_IN_PER_M`, `PRICE_OUT_PER_M` | empty | USD per 1M input/output tokens, used for cost and the cap |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | empty | Optional result store |
| `TRIAGE_API_URL` | `http://127.0.0.1:8000` | Where actors and gateway reach the Python API |
| `DURABLE_ACTORS_CONTROL_PLANE_URL` | `http://127.0.0.1:7100` | Where the gateway reaches the actors |
| `SUPERSERVE_API_KEY`, `FREESTYLE_API_KEY` | empty | Reserved for deployment; not used by the code yet |

## Make targets

| Target | What it does |
|---|---|
| `make setup` | venv, Python deps, Bun deps for actors, gateway and web; creates `.env` |
| `make bench [METHODS=…] [CASES=c01,c02] [RUN_ID=…]` | Runs the benchmark; writes `results/<run_id>/` and refreshes the dashboard's mock results |
| `make board [BOARD=…] [PCB=…]` | Rebuilds `netlist.xml` and `board.json` from KiCad sources (clones the pinned Easyduino commit if absent) |
| `make faults [SEED=2026]` | Regenerates `faults.json` |
| `make api` / `actors` / `gateway` / `web` | Start one service |
| `make dev` | Start all four |
| `make check` | pytest + typecheck + web build |
| `make versions` | Print toolchain versions |

## HTTP API

FastAPI on :8000 (also reachable through the gateway at `:8787/api/*`):

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{ok, board}` |
| GET | `/board/{board_id}` | Public `Board` |
| GET | `/cases/{board_id}` | Public case ids only |
| POST | `/session/{case_id}/panel` | Fresh session: panel measurements, probe budget, panel seconds |
| POST | `/session/{case_id}/probe` `{tp, kind, tp2?}` | One measurement; 409 when the budget is spent, 400 for a bad probe |
| POST | `/triage/{case_id}` `{method}` | `TriageResult` on a fresh session, same protocol as the benchmark |
| POST | `/bench` `{methods, case_ids?}` | Runs a benchmark, returns `{run_id, methods}` |
| GET | `/runs`, `/runs/{run_id or latest}` | Run ids; full results (including truth, post-run only) |

Full contracts: [`CONTRACTS.md`](CONTRACTS.md).

## Live runtime: Durable Actors

- **`BoardSession`** (actor id `${board}-${case}-${method}`) persists stage, status, measurements, probes, hypotheses and errors. Socket messages in are `start`, `probe` and `triage`; it broadcasts the full state after every change, so the dashboard fills stage by stage.
- **`BenchmarkRun`** (actor id = run id) persists its queue, cursor and results. It runs cases one at a time and saves the cursor after each case. On reconnect or a `resume` message it continues from the cursor. No timers or alarms are used.

The gateway grants WebSockets (`POST /socket/{BoardSession|BenchmarkRun}/{id}`) and starts runs (`POST /bench/{run_id}/begin`). API details verified against the running package are in [DECISIONS D11](DECISIONS.md).

## Repository layout

```
pcba-triage/
├── Makefile  .env.example  README.md  CONTRACTS.md  DECISIONS.md  AGENTS.md  CLAUDE.md
├── boards/rp2040_easyduino/   netlist.xml  board.json  roles.yaml  faults.json  NOTICE.md  LICENSE-Easyduino-CERN-OHL-P.txt
├── core/triage/               schemas  netlist  pcb_netlist  pcb_pos  fixture  faults  baselines  agent  bench  report  store  api
├── core/tests/                9 test files, 31 tests
├── prompts/triage_agent.md    agent instructions (hashed into the cache key)
├── shared/types.ts            TypeScript mirror of the contracts
├── actors/                    BoardSession, BenchmarkRun (Durable Actors)
├── gateway/                   Bun gateway + generated actor client
├── web/                       dashboard (Vite + TypeScript, SVG board)
├── supabase/migrations/       001_init.sql
├── results/sample/            committed benchmark report + results.json
├── docs/                      screenshots
└── lanes/                     A–D work-lane briefs used during the build
```

## Tests

| File | Covers |
|---|---|
| `test_board.py` | SI value parsing, board shape, test points, adjacent-pin pairs only on the same package side |
| `test_faults.py` | 6 × 5 faults, deterministic and matching the committed file, opaque case ids, targets taken from the netlist |
| `test_fixture.py` | Healthy board passes the panel; every fault changes something probeable; class signatures; seeded noise; budget enforced |
| `test_bench.py` | Wilson intervals; scoring excludes GND and caps nets; offline bench runs end to end |
| `test_api.py` | Board and cases endpoints; session panel, probe and triage |
| `test_no_leak.py` | No case id or truth keys in the prompt; prompt depends only on measured values; methods never receive the case; Observation has no truth fields |
| `test_agent_offline.py` | Strict output schema, tools, cache key |
| `test_agent_e2e.py` | Full Agents SDK loop against a scripted model: parallel probes, budget, parsing, cost, cache, error counting |

Run them with `make check`, or `make test` for pytest only.

## Run it on another board

1. Put the KiCad project in `boards/_src/<name>` and run `make board BOARD=<id> PCB=<path to .kicad_pcb>`. Install `kicad-cli` to export from the schematic.
2. Write `boards/<id>/roles.yaml`: role overrides for oddly named nets, rail golden values (`nominal_v`, `r_gnd`, `load_ma`), supply limit, MCU refdes and the smoke-test nets (UART TX, LED GPIO, LED node, banner).
3. Run `make faults BOARD=<id>`, then `make bench BOARD=<id>`. The fault picker refuses to run if a class has no candidates on the new board.

The boot-chain model is RP2040-specific (XIP flash → BOOTSEL ROM). Another MCU needs its own boot rules in `fixture.simulate()`.

## Limitations

- **Behavioural fixture.** Signatures are cleaner than a real bench. There are no intermittents, no multiple simultaneous faults, no partial (10–200 Ω) shorts, and no thermal or ESD-damage progression.
- **Rail shorts can't be localized.** Each rail has one golden R-to-GND, so the panel cannot tell which decoupling cap is shorted. RAIL_SHORT refdes accuracy is partly a guess; real benches use thermal imaging or milliohm mapping.
- **Hidden GPIO bridges.** PIN_BRIDGE on unexercised GPIOs has no panel signature; only probes find it, and K=4 is small.
- **Rules and fixture share an author**, and n=30.

## Next steps

Production test beyond bring-up is out of scope: ICT and flying probe, boundary scan, FCT, burn-in, thermal and EMC pre-scan. Within scope:

- A harder fault set: soft shorts, intermittent opens, two-fault cases.
- A rules baseline written blind to the fixture.
- Real UART output from firmware running in Renode.
- More boards (Easyduino UNO, Cicada-HW) and real first-article boards.

## Credits and licenses

- **Board design:** [Easyduino](https://github.com/Hanqaqa/Easyduino) RP2040 by Hanqaqa, CERN Open Hardware Licence v2 – Permissive. The derived `netlist.xml` and `board.json` carry that licence and a notice in [`boards/rp2040_easyduino/`](boards/rp2040_easyduino/NOTICE.md). This project is not affiliated with or endorsed by the Easyduino author.
- **Libraries:** OpenAI Agents SDK, pydantic, FastAPI, Uvicorn, httpx, PyYAML, pytest, Terse Durable Actors, Bun, Vite, TypeScript. Each is under its own licence.
- **Design decisions** made during the build, with their reasons: [`DECISIONS.md`](DECISIONS.md).
