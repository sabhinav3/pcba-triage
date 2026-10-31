"""Triage methods never see the fault case. The agent prompt must carry no truth field."""
import inspect
import json

from triage import agent, baselines
from triage.bench import observe
from triage.fixture import ProbeSession
from triage.schemas import Observation


def prompts(board, cfg, cases):
    out = []
    for c in cases:
        obs, _, _ = observe(board, cfg, c)
        out.append((c, obs, *agent.build_prompt(obs)))
    return out


def test_no_case_id_or_truth_keys_in_prompt(board, cfg, cases):
    for c, obs, instr, user in prompts(board, cfg, cases):
        text = instr + user
        assert c.case_id not in text
        for key in ('"case_id"', '"target"', '"params"', '"cls"', '"seed"', "FaultCase"):
            assert key not in text, key
        assert json.dumps(c.target.model_dump()) not in text
        assert c.cls not in user  # class names appear only in the static instructions


def test_prompt_depends_only_on_measurement_values(board, cfg, cases):
    ps = prompts(board, cfg, cases)
    static = {(instr, user.split("PANEL")[0]) for _, _, instr, user in ps}
    assert len(static) == 1, "board/instruction part of the prompt differs between cases"
    layouts = {tuple((m.stage, m.tp, m.kind) for m in obs.measurements) for _, obs, _, _ in ps}
    assert len(layouts) == 1, "panel layout (which test points/kinds) differs between cases"


def test_method_signatures_take_no_case():
    for fn in (baselines.triage_none, baselines.triage_rules, agent.triage_agent, agent.build_prompt):
        params = inspect.signature(fn).parameters
        assert not any("case" in p for p in params), fn.__name__
        anns = [str(p.annotation) for p in params.values()]
        assert not any("FaultCase" in a for a in anns)
    assert "FaultCase" not in inspect.getsource(agent)


def test_observation_has_no_truth_fields():
    assert set(Observation.model_fields) == {"board_public", "measurements", "probe_budget"}
    assert "_case" not in ProbeSession.__init__.__code__.co_names
