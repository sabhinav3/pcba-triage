"""Agent plumbing that can be verified without a key or network."""
import json

from triage import agent
from triage.bench import observe
from triage.netlist import BoardIndex


def test_output_schema_is_strict():
    from agents import AgentOutputSchema
    s = AgentOutputSchema(agent.AgentAnswer)
    assert s.is_strict_json_schema()
    assert "hypotheses" in json.dumps(s.json_schema())


def test_tools_offline(board, cfg, cases):
    idx = BoardIndex(board)
    n = agent.tool_get_net(idx, "/QSPI_SCLK")
    assert n["role"] == "qspi" and any(x.startswith("U2.52") for x in n["nodes"])
    adj = agent.tool_adjacent_pins(idx, "U2", "/QSPI_SCLK")
    assert {a["adjacent_net"] for a in adj["adjacent"]} == {"/QSPI_SD3", "/QSPI_SD0"}
    _, session, _ = observe(board, cfg, cases[0], budget=1)
    assert "measurement" in agent.tool_probe(session, "TP_3_3V", "v", "")
    assert "error" in agent.tool_probe(session, "TP_3_3V", "v", "")


def test_function_tools_build():
    from agents import function_tool

    @function_tool
    def probe(tp: str, kind: str, tp2: str) -> str:
        """Probe.

        Args:
            tp: test point.
            kind: kind.
            tp2: second tp or "".
        """
        return ""

    assert set(probe.params_json_schema["properties"]) == {"tp", "kind", "tp2"}


def test_cache_key_stable():
    assert agent.cache_key("c01", "m") == agent.cache_key("c01", "m")
    assert agent.cache_key("c01", "m") != agent.cache_key("c02", "m")
