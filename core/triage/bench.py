"""Benchmark: identical panel + identical probe budget for every method, scored against the hidden fault list.

CLI: python -m triage.bench --board rp2040_easyduino --methods none,rules,agent [--cases c01,c02] [--run-id X]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

from . import agent as agent_mod
from .baselines import triage_none, triage_rules
from .faults import load_board, load_faults
from .fixture import Fixture, ProbeSession
from .netlist import BOARDS, BoardIndex, load_cfg
from .schemas import FAULT_CLASSES, PROBE_COST_S, PROMPT_VERSION, Board, FaultCase, Observation, TriageResult
from .store import save_run

PROBE_BUDGET = 4
ALL_METHODS = ["none", "rules", "agent"]


def log(msg: str) -> None:
    print(f"[bench] {msg}", flush=True)


def load_env(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE, # comments). Existing environment wins."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), path)
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            if v.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


def observe(board: Board, cfg: dict, case: FaultCase, budget: int = PROBE_BUDGET) -> Tuple[Observation, ProbeSession, float]:
    fx = Fixture(board, cfg, case)
    panel = fx.panel()
    panel_s = sum(PROBE_COST_S.get(m.kind, 20) for m in panel)
    return Observation(board_public=board, measurements=panel, probe_budget=budget), ProbeSession(fx, budget), panel_s


def run_case(board: Board, cfg: dict, case: FaultCase, method: str, budget: int = PROBE_BUDGET) -> TriageResult:
    obs, session, panel_s = observe(board, cfg, case, budget)
    t0 = time.perf_counter()
    cost, notes = None, []
    try:
        if method == "none":
            hyps = triage_none(obs, session)
        elif method == "rules":
            hyps = triage_rules(obs, session)
        elif method == "agent":
            hyps, cost, notes = agent_mod.triage_agent(obs, session, key=agent_mod.cache_key(case.case_id))
        else:
            raise ValueError(f"unknown method {method}")
    except Exception as e:  # a crashed method scores as a miss, it does not stop the benchmark
        hyps, notes = [], [f"error: {type(e).__name__}: {e}"]
    wall_ms = (time.perf_counter() - t0) * 1000
    return TriageResult(case_id=case.case_id, method=method, hypotheses=hyps[:5], probes=session.probes,
                        probes_used=len(session.probes), bench_seconds=panel_s + session.bench_seconds,
                        wall_ms=round(wall_ms, 2), cost_usd=cost, notes=notes)


def score(case: FaultCase, r: TriageResult, ground: str) -> dict:
    truth = set(case.target.nets) - {ground}

    def hit(h) -> bool:
        return bool((set(h.nets) - {ground}) & truth)

    hs = r.hypotheses
    h1 = hs[0] if hs else None
    return {
        "case_id": case.case_id, "method": r.method, "truth_cls": case.cls, "truth_nets": sorted(truth), "truth_refs": case.target.refs,
        "pred_cls": h1.cls if h1 else None, "pred_nets": h1.nets if h1 else [], "pred_refs": h1.refs if h1 else [],
        "top1_net": bool(h1 and hit(h1)), "top3_net": any(hit(h) for h in hs[:3]),
        "top1_cls": bool(h1 and h1.cls == case.cls), "top1_ref": bool(h1 and set(h1.refs) & set(case.target.refs)),
        "probes_used": r.probes_used, "bench_seconds": r.bench_seconds, "wall_ms": r.wall_ms, "cost_usd": r.cost_usd,
        "error": next((n for n in r.notes if n.startswith("error:")), None),
    }


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 4), round(min(1.0, c + h), 4))


def summarize(method: str, scores: List[dict], skipped: bool = False, reason: Optional[str] = None) -> dict:
    n = len(scores)
    k1 = sum(s["top1_net"] for s in scores)
    k3 = sum(s["top3_net"] for s in scores)
    per_class, confusion = {}, {c: {} for c in FAULT_CLASSES}
    for c in FAULT_CLASSES:
        cs = [s for s in scores if s["truth_cls"] == c]
        per_class[c] = {"n": len(cs), "top1": sum(s["top1_net"] for s in cs), "top3": sum(s["top3_net"] for s in cs),
                        "cls": sum(s["top1_cls"] for s in cs)}
    for s in scores:
        pred = s["pred_cls"] or "(none)"
        confusion[s["truth_cls"]][pred] = confusion[s["truth_cls"]].get(pred, 0) + 1
    costs = [s["cost_usd"] for s in scores if s["cost_usd"] is not None]
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else 0.0  # noqa: E731
    return {
        "method": method, "n": n, "skipped": skipped, "skip_reason": reason,
        "top1": round(k1 / n, 4) if n else 0.0, "top1_k": k1, "top1_ci": wilson(k1, n),
        "top3": round(k3 / n, 4) if n else 0.0, "top3_k": k3, "top3_ci": wilson(k3, n),
        "cls_acc": round(sum(s["top1_cls"] for s in scores) / n, 4) if n else 0.0,
        "ref_acc": round(sum(s["top1_ref"] for s in scores) / n, 4) if n else 0.0,
        "per_class": per_class, "confusion": confusion,
        "mean_probes": mean([s["probes_used"] for s in scores]), "mean_bench_s": mean([s["bench_seconds"] for s in scores]),
        "mean_wall_ms": mean([s["wall_ms"] for s in scores]), "total_cost_usd": round(sum(costs), 4) if costs else None,
        "errors": sum(1 for s in scores if s.get("error")),
    }


def _binom_two_sided(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return round(min(1.0, 2 * p), 4)


def run_bench(board_id: str, methods: List[str], case_ids: Optional[List[str]] = None, run_id: Optional[str] = None,
              budget: int = PROBE_BUDGET, write: bool = True) -> dict:
    load_env()
    board = load_board(board_id)
    cfg = load_cfg(board_id)
    ground = BoardIndex(board).ground
    cases = load_faults(board_id)
    if case_ids:
        cases = [c for c in cases if c.case_id in set(case_ids)]
    run_id = run_id or dt.datetime.now().strftime("run-%Y%m%d-%H%M%S")
    log(f"run {run_id}: board={board_id} cases={len(cases)} methods={','.join(methods)} probe_budget={budget}")

    max_usd = float(os.environ.get("MAX_USD") or 15)
    summaries, all_scores, all_results = [], [], []
    for method in methods:
        if method == "agent":
            ok, why = agent_mod.available()
            cached = agent_mod.cached_count([c.case_id for c in cases])
            if not ok and cached < len(cases):
                log(f"agent skipped: {why} ({cached}/{len(cases)} cases cached). Set OPENAI_API_KEY in .env to run it.")
                summaries.append(summarize("agent", [], skipped=True, reason=f"{why}; {cached}/{len(cases)} cached"))
                continue
            if not ok:
                log(f"agent: no key, replaying {cached} cached results (model={agent_mod.model_name()}, prompt {PROMPT_VERSION})")
            elif cached < len(cases) and agent_mod._cost(1, 1) is None:
                log(f"agent WARNING: PRICE_IN_PER_M / PRICE_OUT_PER_M not set, so MAX_USD={max_usd} cannot be enforced "
                    f"and cost is reported as n/a. Fill both from the OpenAI pricing page to cap spend.")
        scores, spent = [], 0.0
        t_method = time.perf_counter()
        for i, case in enumerate(cases):
            if method == "agent" and spent > max_usd:
                log(f"agent stopped: spent ${spent:.2f} > MAX_USD={max_usd}; {len(cases) - i} cases not run")
                break
            r = run_case(board, cfg, case, method, budget)
            spent += r.cost_usd or 0.0
            s = score(case, r, ground)
            scores.append(s)
            all_results.append(r)
            if method == "agent" or (i + 1) % 10 == 0 or i + 1 == len(cases):
                tag = "HIT " if s["top1_net"] else ("top3" if s["top3_net"] else "miss")
                log(f"{method:5s} {i + 1:2d}/{len(cases)} {case.case_id} {tag} pred={s['pred_cls']} "
                    f"probes={r.probes_used}{' ' + r.notes[0][:80] if r.notes else ''}")
        all_scores += scores
        summ = summarize(method, scores)
        summaries.append(summ)
        log(f"{method:5s} top1={summ['top1_k']}/{summ['n']} top3={summ['top3_k']}/{summ['n']} cls={summ['cls_acc']:.2f} "
            f"probes={summ['mean_probes']} in {time.perf_counter() - t_method:.1f}s")
        if summ["errors"]:
            log(f"{method:5s} WARNING: {summ['errors']}/{summ['n']} cases errored and scored as misses (not cached; rerun retries them)")

    comparisons = []
    by = {(s["method"], s["case_id"]): s for s in all_scores}
    ran = [s["method"] for s in summaries if not s["skipped"] and s["n"]]
    for a, b in (("agent", "rules"), ("rules", "none"), ("agent", "none")):
        if a in ran and b in ran:
            common = [c.case_id for c in cases if (a, c.case_id) in by and (b, c.case_id) in by]
            for metric in ("top1_net", "top3_net"):
                only_a = sum(by[(a, c)][metric] and not by[(b, c)][metric] for c in common)
                only_b = sum(by[(b, c)][metric] and not by[(a, c)][metric] for c in common)
                comparisons.append({"a": a, "b": b, "metric": metric, "n": len(common), "only_a": only_a, "only_b": only_b,
                                    "p_exact": _binom_two_sided(only_a, only_b)})

    bench = {
        "run_id": run_id, "board_id": board_id, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "seed": json.loads((BOARDS / board_id / "faults.json").read_text()).get("seed"),
        "probe_budget": budget, "prompt_version": PROMPT_VERSION, "model": agent_mod.model_name() if "agent" in methods else None,
        "methods": summaries, "comparisons": comparisons, "cases": all_scores,
        "results": [r.model_dump() for r in all_results], "faults": [c.model_dump() for c in cases],
        "panels": {c.case_id: [m.model_dump() for m in Fixture(board, cfg, c).panel()] for c in cases},
    }
    if write:
        from .report import render_report
        md = render_report(bench)
        out = save_run(run_id, bench, md)
        log(f"wrote {out}/report.md and results.json")
    return bench


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="rp2040_easyduino")
    ap.add_argument("--methods", default=",".join(ALL_METHODS))
    ap.add_argument("--cases", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--budget", type=int, default=PROBE_BUDGET)
    args = ap.parse_args()
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    bad = [m for m in methods if m not in ALL_METHODS]
    if bad:
        sys.exit(f"unknown method(s): {bad}; choose from {ALL_METHODS}")
    run_bench(args.board, methods, [c for c in args.cases.split(",") if c] or None, args.run_id or None, args.budget)


if __name__ == "__main__":
    main()
