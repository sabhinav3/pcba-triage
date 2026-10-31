from triage.bench import run_bench, wilson
from triage.schemas import Hypothesis, TriageResult
from triage.bench import score


def test_wilson():
    lo, hi = wilson(15, 30)
    assert (lo, hi) == (0.3315, 0.6685)  # p=0.5, n=30, z=1.96
    assert wilson(0, 30)[0] == 0.0 and wilson(30, 30)[1] == 1.0


def test_scoring_excludes_ground_and_caps_nets(cases):
    c = next(x for x in cases if x.cls == "PIN_BRIDGE")
    gnd_only = TriageResult(case_id=c.case_id, method="none",
                            hypotheses=[Hypothesis(rank=1, cls="PIN_BRIDGE", nets=["GND"], confidence=0.5)])
    assert not score(c, gnd_only, "GND")["top1_net"]
    h = Hypothesis(rank=1, cls="PIN_BRIDGE", nets=["a", "b", "c", "d"], refs=["U1", "U2", "U3", "U4"], confidence=0.5)
    assert len(h.nets) == 3 and len(h.refs) == 3


def test_offline_bench_runs(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = run_bench("rp2040_easyduino", ["none", "rules", "agent"], write=False)
    m = {s["method"]: s for s in out["methods"]}
    assert m["none"]["n"] == 30 and m["rules"]["n"] == 30
    assert m["agent"]["skipped"] or m["agent"]["n"] == 30
    assert all(r["probes_used"] <= 4 for r in out["results"])
    assert m["rules"]["top1"] >= m["none"]["top1"]
