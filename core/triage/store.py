"""Persist benchmark runs: always to results/<run_id>/*.json; also to Supabase (PostgREST) when keys are set.

Supabase is optional: missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY -> JSON only, with one log line.
The service-role key is only read here (server side), never shipped to the browser.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from .netlist import REPO

RESULTS = REPO / "results"


def log(msg: str) -> None:
    print(f"[store] {msg}", flush=True)


def save_run(run_id: str, bench: dict, report_md: str) -> Path:
    out = RESULTS / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(bench, indent=1))
    (out / "report.md").write_text(report_md)
    (RESULTS / "LATEST").write_text(run_id + "\n")
    push_supabase(bench)
    return out


def load_run(run_id: str) -> Optional[dict]:
    if run_id == "latest" and (RESULTS / "LATEST").exists():
        run_id = (RESULTS / "LATEST").read_text().strip()
    p = RESULTS / run_id / "results.json"
    return json.loads(p.read_text()) if p.exists() else None


def list_runs() -> List[str]:
    return sorted([p.parent.name for p in RESULTS.glob("*/results.json")], reverse=True)


def push_supabase(bench: dict) -> bool:
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        log("Supabase not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY empty): JSON only")
        return False
    try:
        import httpx
    except ImportError:
        log("httpx missing: JSON only")
        return False
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    rid = bench["run_id"]
    rows = {
        "runs": [{"run_id": rid, "board_id": bench["board_id"], "created_at": bench["created_at"], "seed": bench["seed"],
                  "probe_budget": bench["probe_budget"], "prompt_version": bench["prompt_version"], "model": bench.get("model"),
                  "summary": bench["methods"], "comparisons": bench.get("comparisons", [])}],
        "cases": [{"run_id": rid, "case_id": c["case_id"], "cls": c["cls"], "target": c["target"], "params": c["params"], "seed": c["seed"]}
                  for c in bench["faults"]],
        "results": [{"run_id": rid, "case_id": r["case_id"], "method": r["method"], "hypotheses": r["hypotheses"],
                     "probes_used": r["probes_used"], "bench_seconds": r["bench_seconds"], "wall_ms": r["wall_ms"],
                     "cost_usd": r["cost_usd"], "notes": r["notes"],
                     "score": next((s for s in bench["cases"] if s["case_id"] == r["case_id"] and s["method"] == r["method"]), None)}
                    for r in bench["results"]],
        "probes": [{"run_id": rid, "case_id": r["case_id"], "method": r["method"], "seq": i, "measurement": m}
                   for r in bench["results"] for i, m in enumerate(r["probes"])],
    }
    try:
        with httpx.Client(timeout=20) as cx:
            for table, data in rows.items():
                if not data:
                    continue
                resp = cx.post(f"{url}/rest/v1/{table}", headers=headers, json=data)
                if resp.status_code >= 300:
                    log(f"Supabase {table}: HTTP {resp.status_code} {resp.text[:200]} (JSON copy kept)")
                    return False
        log(f"pushed run {rid} to Supabase ({sum(len(v) for v in rows.values())} rows)")
        return True
    except Exception as e:
        log(f"Supabase push failed: {e} (JSON copy kept)")
        return False
