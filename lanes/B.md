# Lane B — Triage & Bench (Codex)
Owned: `core/triage/{baselines,agent,bench,report}.py`, `prompts/triage_agent.md`, `core/tests/test_{bench,no_leak,agent_offline}.py`.
Goal: methods `none` (static prior), `rules` (decision tree, fixed probe order), `agent` (OpenAI Agents SDK: get_net, neighbors, adjacent_pins, budgeted probe, structured output, disk cache). Bench + report: top-1/top-3 net accuracy with Wilson 95% CI, per-class, confusion, probes, bench-seconds, wall time, cost, paired comparison.
Gate: `make bench METHODS=none,rules && PYTHONPATH=core .venv/bin/pytest core/tests/test_bench.py core/tests/test_no_leak.py core/tests/test_agent_offline.py -q`
Rules: methods only get `Observation` + `ProbeSession`. No `FaultCase` import in agent.py. Missing key => agent skipped with one log line.
