"""FastAPI service on :8000. Run: uvicorn triage.api:app --port 8000 (PYTHONPATH=core)

GET  /board/{id}                  public board model
GET  /cases/{board_id}            public case ids only (no class / target)
POST /session/{case_id}/panel     standard panel for that case (starts a fresh session, probe budget K)
POST /session/{case_id}/probe     {tp, kind, tp2?} one extra probe, budget enforced
POST /triage/{case_id}            {method} -> TriageResult (fresh session, same protocol as the bench)
POST /bench                       {methods, case_ids?} -> run summary
GET  /runs/{run_id}               results.json ("latest" works)
"""
from __future__ import annotations

import os
from typing import Dict, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .bench import PROBE_BUDGET, load_env, observe, run_bench, run_case
from .faults import load_board, load_faults
from .fixture import BudgetExhausted, ProbeSession
from .netlist import BOARDS, load_cfg
from .schemas import BenchRequest, ProbeRequest, TriageRequest
from .store import list_runs, load_run

load_env()
app = FastAPI(title="pcba-triage", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","), allow_methods=["*"], allow_headers=["*"])

DEFAULT_BOARD = os.environ.get("BOARD_ID", "rp2040_easyduino")
_sessions: Dict[str, Tuple[ProbeSession, list]] = {}


def _case(case_id: str, board_id: str = DEFAULT_BOARD):
    for c in load_faults(board_id):
        if c.case_id == case_id:
            return c
    raise HTTPException(404, f"unknown case {case_id}")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "board": DEFAULT_BOARD}


@app.get("/board/{board_id}")
def get_board(board_id: str) -> dict:
    if not (BOARDS / board_id / "board.json").exists():
        raise HTTPException(404, f"unknown board {board_id}")
    return load_board(board_id).model_dump()


@app.get("/cases/{board_id}")
def get_cases(board_id: str) -> list:
    if not (BOARDS / board_id / "faults.json").exists():
        raise HTTPException(404, f"no faults for {board_id}")
    return [{"case_id": c.case_id} for c in load_faults(board_id)]


@app.post("/session/{case_id}/panel")
def session_panel(case_id: str) -> dict:
    board = load_board(DEFAULT_BOARD)
    obs, session, panel_s = observe(board, load_cfg(DEFAULT_BOARD), _case(case_id), PROBE_BUDGET)
    _sessions[case_id] = (session, obs.measurements)
    return {"case_id": case_id, "measurements": [m.model_dump() for m in obs.measurements], "probe_budget": obs.probe_budget,
            "panel_seconds": panel_s}


@app.post("/session/{case_id}/probe")
def session_probe(case_id: str, req: ProbeRequest) -> dict:
    if case_id not in _sessions:
        session_panel(case_id)
    session, _ = _sessions[case_id]
    try:
        m = session.probe(req.tp, req.kind, req.tp2)
    except BudgetExhausted as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"measurement": m.model_dump(), "probes_left": session.remaining, "bench_seconds": session.bench_seconds}


@app.post("/triage/{case_id}")
def triage(case_id: str, req: TriageRequest) -> dict:
    board = load_board(DEFAULT_BOARD)
    return run_case(board, load_cfg(DEFAULT_BOARD), _case(case_id), req.method, PROBE_BUDGET).model_dump()


@app.post("/bench")
def bench(req: BenchRequest) -> dict:
    out = run_bench(DEFAULT_BOARD, list(req.methods), req.case_ids)
    return {"run_id": out["run_id"], "methods": out["methods"]}


@app.get("/runs")
def runs() -> list:
    return list_runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    r = load_run(run_id)
    if not r:
        raise HTTPException(404, f"no run {run_id}")
    return r
