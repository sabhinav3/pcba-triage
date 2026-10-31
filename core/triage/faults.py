"""Fault injector: 6 classes x 5 cases, targets chosen from the netlist by net role (never hard-coded refs).

CLI: python -m triage.faults --board rp2040_easyduino --seed 2026
"""
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Dict, List, Tuple

from .netlist import BOARDS, BoardIndex, load_cfg, parse_si
from .schemas import FAULT_CLASSES, Board, FaultCase, FaultTarget

PER_CLASS = 5
DEFAULT_SEED = 2026


def load_board(board_id: str) -> Board:
    return Board.model_validate_json((BOARDS / board_id / "board.json").read_text())


def _round_robin(buckets: Dict[str, list], order: List[str], k: int, rng: random.Random) -> List:
    """Pick k items cycling through the bucket order. Buckets with unused items are preferred, so an item
    repeats only when every bucket is exhausted."""
    pools = {b: list(v) for b, v in buckets.items() if v}
    for v in pools.values():
        rng.shuffle(v)
    order = [b for b in order if b in pools]
    picks: List = []
    used = {b: 0 for b in order}
    i = 0
    while len(picks) < k and order:
        fresh = [b for b in order if used[b] < len(pools[b])]
        cycle = fresh or order
        b = cycle[i % len(cycle)] if not fresh else next(
            (order[(i + j) % len(order)] for j in range(len(order)) if order[(i + j) % len(order)] in fresh))
        picks.append(pools[b][used[b] % len(pools[b])])
        used[b] += 1
        i = order.index(b) + 1 if fresh else i + 1
    return picks


def _nets_of(idx: BoardIndex, ref: str) -> List[str]:
    return sorted({p.net for p in idx.comp[ref].pins if idx.role(p.net) != "ground"})


def generate(board: Board, cfg: dict, seed: int = DEFAULT_SEED) -> List[FaultCase]:
    idx = BoardIndex(board)
    rng = random.Random(seed)
    g = idx.ground
    smoke = cfg.get("smoke_test") or {}
    raw: List[Tuple[str, FaultTarget, dict]] = []

    # 1 RAIL_SHORT: rail shorted to GND at one of its decoupling caps
    caps_by_rail: Dict[str, list] = {}
    for c in board.components:
        tp = idx.two_pin(c.ref)
        if c.ref.startswith("C") and tp and g in tp:
            other = tp[0] if tp[1] == g else tp[1]
            if idx.role(other) == "power_rail":
                caps_by_rail.setdefault(other, []).append(c.ref)
    for rail, cap in _round_robin(caps_by_rail and {r: [(r, c) for c in cs] for r, cs in caps_by_rail.items()},
                                  sorted(caps_by_rail), PER_CLASS, rng):
        r_short = round(rng.uniform(0.1, 2.0), 3)
        raw.append(("RAIL_SHORT", FaultTarget(nets=[rail], refs=[cap], pins=[f"{cap}.1"]), {"rail": rail, "cap": cap, "r_short": r_short}))

    # 2 RAIL_OPEN: regulator output open / input open / part missing, on each power-tree edge
    open_b: Dict[str, list] = {}
    for e in board.power_tree:
        n_pins = len(idx.comp[e.ref].pins)
        variants = ["open", "missing"] if e.ref.startswith("D") else (["out_open"] if n_pins > 8 else ["out_open", "in_open", "missing"])
        open_b[e.ref] = [(e, v) for v in variants]
    for e, variant in _round_robin(open_b, [e.ref for e in board.power_tree], PER_CLASS, rng):
        raw.append(("RAIL_OPEN", FaultTarget(nets=[e.in_net, e.out_net], refs=[e.ref]),
                    {"ref": e.ref, "in_net": e.in_net, "out_net": e.out_net, "variant": variant}))

    # 3 WRONG_VALUE: USB series R x100, LED series R x10, crystal load caps x10
    wv: Dict[str, list] = {"usb_series_r": [], "led_r": [], "xtal_cap": []}
    led_gpio = smoke.get("led_gpio")
    led_rs = {lp[2] for lp in idx.led_paths() if lp[2] and led_gpio in (idx.two_pin(lp[2]) or ())}  # LEDs the smoke test exercises
    for ref, a, b, r in idx.resistors:
        if idx.role(a) == "usb" and idx.role(b) == "usb" and r < 100:
            wv["usb_series_r"].append((ref, r, r * 100))
        elif ref in led_rs:
            wv["led_r"].append((ref, r, r * 10))
    for c in board.components:
        tp = idx.two_pin(c.ref)
        if c.ref.startswith("C") and tp and g in tp and any(idx.role(n) == "clock" for n in tp):
            v = parse_si(c.value) or 0.0
            wv["xtal_cap"].append((c.ref, v, v * 10))
    for ref, nominal, actual in _round_robin(wv, ["usb_series_r", "led_r", "xtal_cap"], PER_CLASS, rng):
        params = {"ref": ref, "nominal": nominal, "actual": actual}
        if ref.startswith("C"):
            params["pull_ppm"] = round(rng.uniform(150, 300), 1)
        raw.append(("WRONG_VALUE", FaultTarget(nets=_nets_of(idx, ref), refs=[ref], pins=[]), params))

    # 4 CLOCK_FAIL: crystal missing / cracked / not starting
    xtals = [c.ref for c in board.components if re.match(r"^(Y|X)\d", c.ref) and any(idx.role(p.net) == "clock" for p in c.pins)]
    cf = {x: [(x, v) for v in ("missing", "cracked", "no_start")] for x in xtals}
    for x, variant in _round_robin(cf, xtals, PER_CLASS, rng):
        raw.append(("CLOCK_FAIL", FaultTarget(nets=_nets_of(idx, x), refs=[x]), {"ref": x, "variant": variant}))

    # 5 PIN_BRIDGE: adjacent pins (n, n+1) on a fine-pitch package
    pb: Dict[str, list] = {}
    for c in board.components:
        for p1, p2 in idx.adjacent_pin_pairs(c.ref):
            a, b = idx.pin_net.get((c.ref, p1)), idx.pin_net.get((c.ref, p2))
            if not a or not b or a == b or len(idx.net[a].nodes) < 2 or len(idx.net[b].nodes) < 2:
                continue
            ra, rb = idx.role(a), idx.role(b)
            roles = {ra, rb}
            if roles == {"power_rail", "ground"} or roles == {"ground"}:
                continue  # that is a rail short, not a signal bridge
            if roles <= {"qspi"}:
                bucket = "qspi"
            elif "power_rail" in roles and len(roles) == 1:
                bucket = "power_power"
            elif roles & {"power_rail", "ground"}:
                bucket = "power_signal"
            elif "usb" in roles:
                bucket = "usb"
            elif "clock" in roles:
                bucket = "clock"
            elif roles <= {"gpio", "reset"}:
                bucket = "gpio"
            else:
                bucket = "other"
            pb.setdefault(bucket, []).append((c.ref, p1, p2, a, b))
    for ref, p1, p2, a, b in _round_robin(pb, ["qspi", "gpio", "power_signal", "usb", "clock", "power_power", "other"], PER_CLASS, rng):
        raw.append(("PIN_BRIDGE", FaultTarget(nets=sorted({a, b} - {g}), refs=[ref], pins=[f"{ref}.{p1}", f"{ref}.{p2}"]),
                    {"ref": ref, "pin_a": p1, "pin_b": p2, "net_a": a, "net_b": b, "r_bridge": round(rng.uniform(0.05, 0.3), 3)}))

    # 6 SIGNAL_OPEN: lifted IC pin on a qspi / usb / exercised-gpio / clock net
    so: Dict[str, list] = {}
    exercised = {smoke.get("uart_tx"), smoke.get("led_gpio")} - {None}
    for n in board.nets:
        if n.role not in ("qspi", "usb", "gpio", "clock") or len(n.nodes) < 2:
            continue
        if n.role == "gpio" and n.name not in exercised:
            continue
        for nd in n.nodes:
            if idx.is_ic(nd.ref):
                so.setdefault(n.role, []).append((nd.ref, nd.pin, n.name))
    for ref, pin, net in _round_robin(so, ["qspi", "usb", "gpio", "clock"], PER_CLASS, rng):
        raw.append(("SIGNAL_OPEN", FaultTarget(nets=[net], refs=[ref], pins=[f"{ref}.{pin}"]), {"ref": ref, "pin": pin, "net": net}))

    counts = {c: sum(1 for r in raw if r[0] == c) for c in FAULT_CLASSES}
    short = [c for c, k in counts.items() if k != PER_CLASS]
    if short:
        raise RuntimeError(f"could not build {PER_CLASS} candidates for: {short} ({counts})")
    rng.shuffle(raw)  # case ids must not reveal the class
    return [FaultCase(case_id=f"c{i + 1:02d}", cls=cls, target=t, params=params, seed=seed * 1000 + i + 1)
            for i, (cls, t, params) in enumerate(raw)]


def load_faults(board_id: str) -> List[FaultCase]:
    data = json.loads((BOARDS / board_id / "faults.json").read_text())
    return [FaultCase.model_validate(c) for c in data["cases"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="rp2040_easyduino")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args()
    board = load_board(args.board)
    cases = generate(board, load_cfg(args.board), args.seed)
    out = BOARDS / args.board / "faults.json"
    out.write_text(json.dumps({"board_id": args.board, "seed": args.seed, "per_class": PER_CLASS,
                               "cases": [c.model_dump() for c in cases]}, indent=1))
    by = {}
    for c in cases:
        by.setdefault(c.cls, []).append(f"{c.case_id}:{'/'.join(c.target.refs)}[{','.join(c.target.nets)}]")
    print(f"[faults] {len(cases)} cases (seed {args.seed}) -> {out}")
    for k in FAULT_CLASSES:
        print(f"[faults]   {k:12s} {'  '.join(by.get(k, []))}")


if __name__ == "__main__":
    main()
