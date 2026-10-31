"""LLM triage method: OpenAI Agents SDK (openai-agents) + netlist tools + budgeted probe tool + structured output.

Optional at runtime: without OPENAI_API_KEY (or without the package) `available()` is False and the bench skips
this method with a clear log line. The prompt is built ONLY from an Observation (see build_prompt); the fault
case never reaches this module (tests/test_no_leak.py checks that).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from .fixture import BudgetExhausted, ProbeSession
from .netlist import REPO, BoardIndex
from .schemas import PROMPT_VERSION, Board, FaultClass, Hypothesis, Measurement, Observation, ProbeRequest

PROMPT_FILE = REPO / "prompts" / "triage_agent.md"
CACHE_DIR = REPO / "results" / "cache" / "agent"
MAX_TURNS = 16
# Tests only: an `agents.Model` instance used in place of the OpenAI model (core/tests/test_agent_e2e.py).
MODEL_OVERRIDE = None


class AgentHypothesis(BaseModel):
    cls: FaultClass
    nets: List[str]
    refs: List[str]
    confidence: float
    evidence: List[str]
    next_tp: str
    next_kind: str
    next_tp2: str


class AgentAnswer(BaseModel):
    hypotheses: List[AgentHypothesis]


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL") or "gpt-5-mini"


def available() -> Tuple[bool, str]:
    if not os.environ.get("OPENAI_API_KEY"):
        return False, "OPENAI_API_KEY not set"
    try:
        import agents  # noqa: F401
    except Exception as e:  # pragma: no cover
        return False, f"openai-agents not importable: {e}"
    return True, ""


def prompt_fingerprint() -> str:
    return hashlib.sha256((PROMPT_VERSION + "\n" + PROMPT_FILE.read_text()).encode()).hexdigest()[:12]


def cache_key(case_id: str, model: Optional[str] = None) -> str:
    """hash(case, method, model, prompt_version). Used only for the on-disk cache, never sent to the model."""
    return hashlib.sha256(f"{case_id}|agent|{model or model_name()}|{PROMPT_VERSION}|{prompt_fingerprint()}".encode()).hexdigest()[:32]


# ---------------- prompt ----------------
def board_brief(board: Board) -> dict:
    nom = {k: {f: v for f, v in n.model_dump().items() if v is not None} for k, n in board.nominal.items()}
    pins_by_ref = {c.ref: {p.pin: p.name for p in c.pins} for c in board.components}
    return {
        "board_id": board.board_id,
        "components": [f"{c.ref} {c.value} ({c.footprint.split(':')[-1]})" for c in board.components if c.pins],
        "nets": [{"name": n.name, "role": n.role,
                  "nodes": [f"{nd.ref}.{nd.pin}" + (f"({pins_by_ref.get(nd.ref, {}).get(nd.pin)})"
                            if pins_by_ref.get(nd.ref, {}).get(nd.pin) not in (None, nd.pin) else "") for nd in n.nodes]}
                 for n in board.nets if len(n.nodes) >= 2],
        "power_tree": [f"{e.in_net} --{e.ref}--> {e.out_net} ({e.nominal_v} V)" for e in board.power_tree],
        "testpoints": {t.tp: {"net": t.net, "kinds": t.kinds} for t in board.testpoints},
        "golden": nom,
    }


def format_measurement(m: Measurement) -> str:
    v = m.value
    if isinstance(v, float):
        v = "OPEN(1e9)" if v >= 1e9 else f"{v:.6g}"
    elif v == "":
        v = '""'
    where = f"{m.tp}" + (f"<->{m.tp2}" if m.tp2 else "")
    return f"{m.stage} {m.kind:8s} {where:28s} = {v} {m.unit}".rstrip()


def build_prompt(obs: Observation) -> Tuple[str, str]:
    """(instructions, user_input). A pure function of the Observation."""
    instructions = PROMPT_FILE.read_text()
    panel = "\n".join(format_measurement(m) for m in obs.measurements)
    user = (
        "BOARD\n" + json.dumps(board_brief(obs.board_public), separators=(",", ":")) +
        "\n\nPANEL (standard bring-up measurements, already taken)\n" + panel +
        f"\n\nPROBE BUDGET: {obs.probe_budget} extra probes.\n"
        "Diagnose the fault. Use the tools, then return your ranked hypotheses."
    )
    return instructions, user


# ---------------- tools (pure helpers, testable offline) ----------------
def tool_get_net(idx: BoardIndex, name: str) -> dict:
    n = idx.net.get(name)
    if not n:
        close = [x for x in idx.net if name.lower().strip("/+") in x.lower()][:8]
        return {"error": f"no net {name!r}", "did_you_mean": close}
    names = {(c.ref, p.pin): p.name for c in idx.board.components for p in c.pins}
    return {"name": n.name, "role": n.role, "tp": idx.tp_of_net.get(n.name),
            "nodes": [f"{nd.ref}.{nd.pin}({names.get((nd.ref, nd.pin), nd.pin)})" for nd in n.nodes],
            "golden": {k: v for k, v in idx.board.nominal[n.name].model_dump().items() if v is not None} if n.name in idx.board.nominal else {}}


def tool_adjacent_pins(idx: BoardIndex, ref: str, net: str) -> dict:
    if ref not in idx.comp:
        return {"error": f"no component {ref!r}"}
    pairs = idx.adjacent_pin_pairs(ref)
    if not pairs:
        return {"error": f"{ref} is not a fine-pitch package with numbered sides"}
    mine = {p.pin for p in idx.comp[ref].pins if p.net == net} if net else None
    out = []
    for a, b in pairs:
        for x, y in ((a, b), (b, a)):
            if mine is None or x in mine:
                out.append({"pin": f"{ref}.{x}", "net": idx.pin_net.get((ref, x)), "adjacent_pin": f"{ref}.{y}",
                            "adjacent_net": idx.pin_net.get((ref, y)), "adjacent_tp": idx.tp_of_net.get(idx.pin_net.get((ref, y), ""))})
    return {"ref": ref, "net": net or None, "adjacent": out[:60]}


def tool_probe(session: ProbeSession, tp: str, kind: str, tp2: str) -> dict:
    try:
        m = session.probe(tp, kind, tp2 or None)
    except BudgetExhausted as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e), "probes_left": session.remaining}
    return {"measurement": format_measurement(m), "value": m.value, "unit": m.unit, "probes_left": session.remaining}


# ---------------- run ----------------
def _to_hypotheses(ans: AgentAnswer, idx: BoardIndex) -> List[Hypothesis]:
    out: List[Hypothesis] = []
    for h in ans.hypotheses[:5]:
        nets = [n for n in h.nets if n in idx.net and idx.role(n) != "ground"]
        refs = [r for r in h.refs if r in idx.comp]
        nxt = None
        if h.next_tp and h.next_kind in ("v", "r_gnd", "r_pair", "freq", "logic", "i_supply", "uart", "usb_enum") and h.next_tp in idx.tp:
            nxt = ProbeRequest(tp=h.next_tp, kind=h.next_kind, tp2=h.next_tp2 or None)
        out.append(Hypothesis(rank=len(out) + 1, cls=h.cls, nets=nets, refs=refs,
                              confidence=max(0.0, min(1.0, float(h.confidence))), evidence=h.evidence[:6], next_probe=nxt))
    return out


def triage_agent(obs: Observation, session: ProbeSession, key: Optional[str] = None) -> Tuple[List[Hypothesis], Optional[float], List[str]]:
    """Returns (hypotheses, cost_usd, notes). Probes are recorded on `session`."""
    if key:
        hit = _cache_get(key)
        if hit:
            session.probes = [Measurement.model_validate(m) for m in hit["probes"]]
            session.bench_seconds = hit["bench_seconds"]
            return [Hypothesis.model_validate(h) for h in hit["hypotheses"]], hit.get("cost_usd"), ["cache hit"] + hit.get("notes", [])
    ok, why = available()
    if not ok:
        raise RuntimeError(f"agent unavailable: {why}")

    from agents import Agent, RunConfig, Runner, function_tool

    idx = BoardIndex(obs.board_public)
    instructions, user = build_prompt(obs)

    @function_tool
    def get_net(name: str) -> str:
        """Nodes (REF.PIN(pin name)), role, test point and golden readings of one net.

        Args:
            name: exact net name, e.g. "+3.3V" or "QSPI_SCLK".
        """
        return json.dumps(tool_get_net(idx, name))

    @function_tool
    def neighbors(ref_or_net: str) -> str:
        """Nets of a component, or the components on a net plus nets one small part away.

        Args:
            ref_or_net: a refdes like "R4" or a net name.
        """
        return json.dumps(idx.neighbors(ref_or_net) or {"error": f"unknown {ref_or_net!r}"})

    @function_tool
    def adjacent_pins(ref: str, net: str) -> str:
        """Physically adjacent pins (n, n+1 on the same package side) of a fine-pitch IC.

        Args:
            ref: IC refdes, e.g. the MCU.
            net: only list neighbours of this net's pins on that IC; "" lists every adjacent pair.
        """
        return json.dumps(tool_adjacent_pins(idx, ref, net))

    @function_tool
    def probe(tp: str, kind: str, tp2: str) -> str:
        """Take one bench measurement. Hard budget; returns the reading and probes left.

        Args:
            tp: test point id from the board's testpoints.
            kind: one of v, r_gnd, r_pair, freq, logic, i_supply.
            tp2: second test point for r_pair, otherwise "".
        """
        return json.dumps(tool_probe(session, tp, kind, tp2))

    model = model_name()
    agent = Agent(name="pcba-triage", instructions=instructions, tools=[get_net, neighbors, adjacent_pins, probe],
                  model=MODEL_OVERRIDE or model, output_type=AgentAnswer)
    t0 = time.time()
    result = Runner.run_sync(agent, user, max_turns=MAX_TURNS, run_config=RunConfig(tracing_disabled=True))
    ans: AgentAnswer = result.final_output
    usage = result.context_wrapper.usage
    notes = [f"model={model} in_tok={usage.input_tokens} out_tok={usage.output_tokens} requests={usage.requests} "
             f"llm_s={time.time() - t0:.1f}"]
    cost = _cost(usage.input_tokens, usage.output_tokens)
    if cost is None:
        notes.append("cost n/a: PRICE_IN_PER_M / PRICE_OUT_PER_M not set")
    hyps = _to_hypotheses(ans, idx)
    if key:
        _cache_put(key, {"hypotheses": [h.model_dump() for h in hyps], "probes": [m.model_dump() for m in session.probes],
                         "bench_seconds": session.bench_seconds, "cost_usd": cost, "notes": notes,
                         "tokens": {"in": usage.input_tokens, "out": usage.output_tokens}})
    return hyps, cost, notes


def _cost(tok_in: int, tok_out: int) -> Optional[float]:
    try:
        pin, pout = float(os.environ.get("PRICE_IN_PER_M", "")), float(os.environ.get("PRICE_OUT_PER_M", ""))
    except ValueError:
        return None
    return round(tok_in / 1e6 * pin + tok_out / 1e6 * pout, 5)


def _cache_get(key: str) -> Optional[dict]:
    p = CACHE_DIR / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _cache_put(key: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(data, indent=1))


def cached_count(case_ids: List[str]) -> int:
    return sum(1 for c in case_ids if (CACHE_DIR / f"{cache_key(c)}.json").exists())


__all__ = ["available", "build_prompt", "triage_agent", "cache_key", "cached_count", "AgentAnswer",
           "tool_get_net", "tool_adjacent_pins", "tool_probe"]
