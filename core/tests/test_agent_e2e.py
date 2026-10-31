"""End-to-end agent run with no key and no network.

A scripted `agents.Model` replaces the OpenAI model. Everything else is real: Agent, Runner, function tools,
structured output parsing, the budgeted ProbeSession, the cache, and bench.run_case(). This is the path that
runs for the first time when someone adds OPENAI_API_KEY, so it must not be untested.
"""
import json
import threading
import time

import pytest
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from triage import agent
from triage.bench import observe, run_case
from triage.fixture import BudgetExhausted


def _call(i: int, _tool: str, **args) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(type="function_call", call_id=f"call_{i}", name=_tool, arguments=json.dumps(args))


def _message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(id="msg_1", type="message", role="assistant", status="completed",
                                 content=[ResponseOutputText(type="output_text", text=text, annotations=[])])


class ScriptedModel(Model):
    """Turn 1: netlist tools + 6 probes in ONE turn (parallel tool calls). Turn 2: the structured answer."""

    def __init__(self):
        self.turns = 0
        self.seen_instructions = None
        self.seen_input = []
        self.seen_tools = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, *, previous_response_id=None, conversation_id=None, prompt=None):
        self.turns += 1
        self.seen_instructions = system_instructions
        self.seen_input.append(json.dumps(input, default=str) if not isinstance(input, str) else input)
        self.seen_tools = [t.name for t in tools]
        usage = Usage(requests=1, input_tokens=1000, output_tokens=100, total_tokens=1100)
        if self.turns == 1:
            out = [_call(1, "get_net", name="/QSPI_SCLK"),
                   _call(2, "adjacent_pins", ref="U2", net="/QSPI_SCLK"),
                   _call(3, "neighbors", ref_or_net="R4")]
            out += [_call(10 + k, "probe", tp="TP_3_3V", kind="v", tp2="") for k in range(6)]
        else:
            out = [_message(json.dumps({"hypotheses": [
                {"cls": "SIGNAL_OPEN", "nets": ["/QSPI_SCLK", "NOT_A_NET"], "refs": ["U2", "U99"], "confidence": 1.7,
                 "evidence": ["QSPI_SCLK floating", "RP2 Boot"], "next_tp": "TP_3_3V", "next_kind": "v", "next_tp2": ""},
                {"cls": "PIN_BRIDGE", "nets": ["/QSPI_SCLK", "/QSPI_SD3"], "refs": ["U2"], "confidence": 0.2,
                 "evidence": ["adjacent"], "next_tp": "", "next_kind": "", "next_tp2": ""},
            ]}))]
        return ModelResponse(output=out, usage=usage, response_id=None)

    def stream_response(self, *a, **k):  # pragma: no cover - the agent uses run_sync
        raise NotImplementedError


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    m = ScriptedModel()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("PRICE_IN_PER_M", "0.25")
    monkeypatch.setenv("PRICE_OUT_PER_M", "2.0")
    monkeypatch.setattr(agent, "MODEL_OVERRIDE", m)
    monkeypatch.setattr(agent, "CACHE_DIR", tmp_path / "cache")
    return m


def test_agent_runs_end_to_end(board, cfg, cases, scripted):
    case = cases[0]
    r = run_case(board, cfg, case, "agent", budget=4)
    assert not any(n.startswith("error") for n in r.notes), r.notes
    assert scripted.turns == 2
    assert set(scripted.seen_tools) == {"get_net", "neighbors", "adjacent_pins", "probe"}
    # 6 parallel probe calls in one turn, budget 4: exactly 4 recorded, the rest got a budget error back
    assert r.probes_used == 4 and len(r.probes) == 4
    turn2 = scripted.seen_input[-1]
    assert turn2.count("probe budget of 4 used up") == 2
    # structured output was parsed and sanitised: unknown nets/refs dropped, confidence clamped
    h1 = r.hypotheses[0]
    assert h1.cls == "SIGNAL_OPEN" and h1.nets == ["/QSPI_SCLK"] and h1.refs == ["U2"] and h1.confidence == 1.0
    assert h1.next_probe is not None and r.hypotheses[1].next_probe is None
    assert r.cost_usd == pytest.approx(2 * (1000 * 0.25 + 100 * 2.0) / 1e6)


def test_agent_prompt_actually_sent_has_no_truth(board, cfg, cases, scripted):
    """Checks the bytes the model received, not just build_prompt()."""
    case = cases[1]
    run_case(board, cfg, case, "agent", budget=4)
    sent = scripted.seen_instructions + "".join(scripted.seen_input)
    assert case.case_id not in sent
    assert json.dumps(case.target.model_dump()) not in sent
    for k in ('"cls"', '"target"', '"params"', '"seed"'):
        assert k not in scripted.seen_input[0]


def test_agent_cache_replays_without_model(board, cfg, cases, scripted, monkeypatch):
    case = cases[2]
    first = run_case(board, cfg, case, "agent", budget=4)
    assert scripted.turns == 2
    monkeypatch.delenv("OPENAI_API_KEY")
    second = run_case(board, cfg, case, "agent", budget=4)
    assert scripted.turns == 2  # model not called again
    assert second.notes[0] == "cache hit"
    assert [h.model_dump() for h in second.hypotheses] == [h.model_dump() for h in first.hypotheses]
    assert second.probes_used == first.probes_used == 4


class FailingModel(ScriptedModel):
    async def get_response(self, *a, **k):
        raise RuntimeError("simulated 429 rate limit")


def test_agent_errors_are_counted_not_cached(board, cfg, cases, monkeypatch, tmp_path):
    from triage.bench import score, summarize
    from triage.netlist import BoardIndex
    from triage.report import render_report
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(agent, "MODEL_OVERRIDE", FailingModel())
    monkeypatch.setattr(agent, "CACHE_DIR", tmp_path / "cache")
    r = run_case(board, cfg, cases[3], "agent", budget=4)
    assert r.hypotheses == [] and r.notes[0].startswith("error: RuntimeError")
    assert not (tmp_path / "cache").exists()
    s = score(cases[3], r, BoardIndex(board).ground)
    summ = summarize("agent", [s])
    assert summ["errors"] == 1 and summ["top1_k"] == 0
    md = render_report({"run_id": "t", "board_id": board.board_id, "created_at": "x", "seed": 1, "probe_budget": 4,
                        "prompt_version": "v1", "model": "m", "methods": [summ], "comparisons": [], "cases": [s],
                        "results": [r.model_dump()], "faults": [cases[3].model_dump()], "panels": {}})
    assert "1/1 cases errored" in md


def test_probe_budget_holds_under_threads(board, cfg, cases):
    """The SDK runs sync tools via asyncio.to_thread, so parallel probe calls race. Budget must still hold."""
    _, session, _ = observe(board, cfg, cases[0], budget=4)
    real = session._measure

    def slow(*a, **k):
        time.sleep(0.02)
        return real(*a, **k)

    session._measure = slow
    errors = []

    def worker():
        try:
            session.probe("TP_3_3V", "v")
        except BudgetExhausted as e:
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(session.probes) == 4 and len(errors) == 6
