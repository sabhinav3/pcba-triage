from fastapi.testclient import TestClient

from triage.api import app

client = TestClient(app)


def test_board_and_cases():
    assert client.get("/board/rp2040_easyduino").json()["board_id"] == "rp2040_easyduino"
    cases = client.get("/cases/rp2040_easyduino").json()
    assert len(cases) == 30 and all(set(c) == {"case_id"} for c in cases)


def test_session_and_triage():
    p = client.post("/session/c01/panel").json()
    assert p["probe_budget"] == 4 and p["measurements"]
    r = client.post("/session/c01/probe", json={"tp": "TP_3_3V", "kind": "v"})
    assert r.status_code == 200 and r.json()["probes_left"] == 3
    assert client.post("/session/c01/probe", json={"tp": "NOPE", "kind": "v"}).status_code == 400
    t = client.post("/triage/c01", json={"method": "rules"}).json()
    assert t["method"] == "rules" and len(t["hypotheses"]) <= 5
