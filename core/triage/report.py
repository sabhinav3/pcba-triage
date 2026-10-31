"""results.json -> report.md"""
from __future__ import annotations

from typing import Dict, List

from .schemas import FAULT_CLASSES

SHORT = {"RAIL_SHORT": "R_SHORT", "RAIL_OPEN": "R_OPEN", "WRONG_VALUE": "WRONG_V", "CLOCK_FAIL": "CLOCK",
         "PIN_BRIDGE": "BRIDGE", "SIGNAL_OPEN": "SIG_OPEN", "(none)": "(none)"}


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _ci(ci) -> str:
    return f"[{100 * ci[0]:.0f}–{100 * ci[1]:.0f}%]"


def _p(p: float) -> str:
    return "<0.0001" if p < 1e-4 else f"{p:.3g}"


def verdict(bench: dict) -> str:
    m = {s["method"]: s for s in bench["methods"]}
    a, r = m.get("agent"), m.get("rules")
    if not a or a["skipped"] or not a["n"]:
        why = a["skip_reason"] if a else "not requested"
        return f"**Agent not evaluated in this run** ({why}). Rules vs static prior only."
    if not r or not r["n"]:
        return "Rules baseline not run; no agent-vs-rules verdict."
    comp = next((c for c in bench.get("comparisons", []) if c["a"] == "agent" and c["b"] == "rules" and c["metric"] == "top1_net"), None)
    sig = comp and comp["p_exact"] < 0.05
    if a["top1"] > r["top1"]:
        qual = "and the paired difference is significant" if sig else "but the paired difference is NOT significant at n=30"
        return (f"**Agent beats rules on top-1 net accuracy on the point estimate** ({_pct(a['top1'])} vs {_pct(r['top1'])}), {qual} "
                f"(discordant cases: agent-only {comp['only_a']}, rules-only {comp['only_b']}, exact p={_p(comp['p_exact'])}).")
    if a["top1"] == r["top1"]:
        return f"**Agent ties rules** on top-1 net accuracy ({_pct(a['top1'])}). No evidence the LLM adds accuracy on this benchmark."
    return (f"**Agent does NOT beat rules**: top-1 net accuracy {_pct(a['top1'])} vs {_pct(r['top1'])}"
            + (f" (agent-only {comp['only_a']}, rules-only {comp['only_b']}, exact p={_p(comp['p_exact'])})." if comp else "."))


def render_report(bench: dict) -> str:
    ms = [s for s in bench["methods"]]
    ran = [s for s in ms if not s["skipped"] and s["n"]]
    L: List[str] = []
    L.append(f"# PCBA triage benchmark — {bench['board_id']} — {bench['run_id']}\n")
    L.append(f"Created {bench['created_at']} · fault seed {bench['seed']} · {len(bench['faults'])} injected faults "
             f"(6 classes × 5) · extra-probe budget K={bench['probe_budget']} · prompt {bench['prompt_version']}"
             + (f" · model `{bench['model']}`" if bench.get("model") else "") + "\n")
    L.append(verdict(bench) + "\n")
    L.append("## Summary\n")
    L.append("| method | n | top-1 net | 95% CI (Wilson) | top-3 net | 95% CI | class acc | refdes acc | mean extra probes | mean bench s | mean wall ms | LLM cost |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in ms:
        if s["skipped"] or not s["n"]:
            L.append(f"| {s['method']} | 0 | skipped: {s['skip_reason']} |  |  |  |  |  |  |  |  |  |")
            continue
        cost = "n/a" if s["total_cost_usd"] is None else f"${s['total_cost_usd']:.3f}"
        if s["method"] != "agent":
            cost = "$0"
        L.append(f"| {s['method']} | {s['n']} | {s['top1_k']}/{s['n']} ({_pct(s['top1'])}) | {_ci(s['top1_ci'])} | "
                 f"{s['top3_k']}/{s['n']} ({_pct(s['top3'])}) | {_ci(s['top3_ci'])} | {_pct(s['cls_acc'])} | {_pct(s['ref_acc'])} | "
                 f"{s['mean_probes']} | {s['mean_bench_s']} | {s['mean_wall_ms']} | {cost} |")
    for s in ran:
        if s.get("errors"):
            L.append(f"\n**{s['method']}: {s['errors']}/{s['n']} cases errored** (API failure, timeout or unparseable output) and are "
                     "scored as misses above. Errored cases are not cached, so rerunning `make bench` retries only those.")
    L.append("\nn=30 is small: a 95% Wilson interval at 50% spans roughly ±17 points, so differences under ~25 points between "
             "methods are not distinguishable by the intervals alone. The paired comparison below uses the same cases for both methods.\n")
    if bench.get("comparisons"):
        L.append("### Paired comparison (same cases)\n")
        L.append("| A vs B | metric | A-only hits | B-only hits | exact two-sided p |")
        L.append("|---|---|---|---|---|")
        for c in bench["comparisons"]:
            L.append(f"| {c['a']} vs {c['b']} | {c['metric']} | {c['only_a']} | {c['only_b']} | {_p(c['p_exact'])} |")
        L.append("")
    if ran:
        L.append("## Per-class accuracy (top-1 net / top-3 net / class, out of 5)\n")
        L.append("| class | " + " | ".join(s["method"] for s in ran) + " |")
        L.append("|---|" + "---|" * len(ran))
        for c in FAULT_CLASSES:
            cells = []
            for s in ran:
                pc = s["per_class"][c]
                cells.append(f"{pc['top1']}/{pc['top3']}/{pc['cls']} of {pc['n']}")
            L.append(f"| {c} | " + " | ".join(cells) + " |")
        L.append("")
        L.append("## Class confusion (rows = injected class, columns = top-1 predicted class)\n")
        cols = FAULT_CLASSES + ["(none)"]
        for s in ran:
            L.append(f"**{s['method']}**\n")
            L.append("| truth \\ pred | " + " | ".join(SHORT[c] for c in cols) + " |")
            L.append("|---|" + "---|" * len(cols))
            for t in FAULT_CLASSES:
                row = s["confusion"].get(t, {})
                L.append(f"| {SHORT[t]} | " + " | ".join(str(row.get(c, 0)) if row.get(c, 0) else "·" for c in cols) + " |")
            L.append("")
    L.append("## Per-case results\n")
    by: Dict[tuple, dict] = {(s["method"], s["case_id"]): s for s in bench["cases"]}
    L.append("| case | injected | truth nets | " + " | ".join(f"{s['method']} top-1 (probes)" for s in ran) + " |")
    L.append("|---|---|---|" + "---|" * len(ran))
    for f in sorted(bench["faults"], key=lambda f: f["case_id"]):
        cells = []
        for s in ran:
            x = by.get((s["method"], f["case_id"]))
            if not x:
                cells.append("—")
                continue
            mark = "✅" if x["top1_net"] else ("🟡" if x["top3_net"] else "❌")
            cells.append(f"{mark} {SHORT.get(x['pred_cls'] or '(none)')} {','.join(x['pred_nets'][:2])} ({x['probes_used']})")
        params = f.get("params", {})
        detail = params.get("variant") or (f"{params.get('nominal'):g}→{params.get('actual'):g}" if "actual" in params else "")
        L.append(f"| {f['case_id']} | {f['cls']} {'/'.join(f['target']['refs'])} {detail} | {', '.join(f['target']['nets'])} | " + " | ".join(cells) + " |")
    L.append("\n✅ top-1 net hit · 🟡 hit in top-3 · ❌ miss\n")
    L.append("## Method and scoring\n")
    L.append("- Faults are injected into a behavioral model of the board (power tree, current-limited supply, DMM readings, crystal, "
             "boot chain, UART, LED, USB). It is not SPICE. Analog readings carry seeded ~1% Gaussian noise (5 ppm on frequency).")
    L.append("- Every method sees the same standard panel (S1–S5, S7, S8) and may take up to K extra probes, enforced in code. "
             "Bench time per probe: v 15 s, r_gnd/r_pair 20 s, freq 45 s, logic 10 s, i_supply 10 s. "
             "\"mean bench s\" includes the fixed panel time, which is identical for every method.")
    L.append("- A hypothesis hits if its nets overlap the injected fault's nets. GND is excluded from both sides. Each hypothesis is "
             "capped at 3 nets and 3 refdes so listing many nets cannot buy hits. top-k = any hit in the first k hypotheses.")
    L.append("- Class accuracy = top-1 class matches. Refdes accuracy = top-1 refdes overlap; it is low by construction for rail shorts "
             "(one rail has many decoupling caps and the panel cannot localize which).")
    L.append("- `none` ignores all measurements (static prior). `rules` is a deterministic decision tree with a fixed probe order per branch. "
             "`agent` is an LLM with netlist tools and the same probe tool; its results are cached on disk by hash(case, method, model, prompt version).")
    return "\n".join(L) + "\n"
