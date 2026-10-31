"""KiCad netlist (kicadxml) -> Board model, plus BoardIndex helpers shared by fixture, faults, baselines and agent.

CLI:  python -m triage.netlist build --board rp2040_easyduino --pcb <file.kicad_pcb>
"""
from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from functools import cached_property
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .schemas import Board, Component, Net, NetNode, Nominal, Outline, Pin, PowerEdge, TestPoint

REPO = Path(__file__).resolve().parents[2]
BOARDS = REPO / "boards"

ROLE_PATTERNS: List[Tuple[str, str]] = [
    ("ground", r"^/?(GND|AGND|DGND|GNDA|GNDD|VSS|0V)$"),
    ("power_rail", r"^/?(\+.+|VBUS|VSYS|VCC.*|VDD.*|VIN|\d+V\d*)$"),
    ("clock", r"(?i)(^|/)(xin|xout|xtal|osc|clk_?in|mco)"),
    ("qspi", r"(?i)qspi"),
    ("usb", r"(?i)(usb|(^|/)cc\d$)"),
    ("reset", r"(?i)(^|/)(run|n?reset|n?rst)$"),
    ("gpio", r"(?i)gpio"),
]
OUT_PIN_RE = re.compile(r"(?i)(^|_)(VO|VOUT|OUT|K)$")
IN_PIN_RE = re.compile(r"(?i)(^|_)(VI|VIN|IN|A)$")
SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def parse_si(value: str) -> Optional[float]:
    """'5.1k' -> 5100, '27' -> 27, '100nF' -> 1e-7, '4k7' -> 4700, '12MHz' -> 1.2e7."""
    s = value.strip().replace("Ω", "").replace("ohm", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([pnuµmkKMG]?)(\d*)", s)
    if not m:
        return None
    base, mult, tail = m.groups()
    if tail and mult:  # 4k7 style
        base = f"{base}.{tail}"
    f = float(base) * SI.get(mult, 1.0)
    if s[m.end():m.end() + 1] == "M" and not mult:  # '12MHz' parsed as 12 + 'MHz'
        f *= 1e6
    return f


def classify_net(name: str, overrides: Dict[str, str]) -> str:
    if name in overrides:
        return overrides[name]
    if name.startswith("unconnected-"):
        return "other"
    for role, pat in ROLE_PATTERNS:
        if re.search(pat, name):
            return role
    return "other"


def tp_name(net: str) -> str:
    return "TP_" + (re.sub(r"[^A-Za-z0-9]+", "_", net).strip("_") or "NET")


def load_cfg(board_id: str) -> dict:
    p = BOARDS / board_id / "roles.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def _tp_rank(ref: str, n_pins: int) -> Tuple[int, str]:
    prefix = re.match(r"^[A-Za-z]+", ref).group(0) if re.match(r"^[A-Za-z]+", ref) else ref
    num = int(re.sub(r"\D", "", ref) or 0)
    if prefix == "TP":
        r = -1  # a real test point pad on the board
    elif prefix in ("R", "C", "L"):
        r = 0
    elif prefix in ("J", "P"):
        r = 1
    elif prefix in ("D", "Y", "X", "SW", "F", "FB"):
        r = 2
    elif n_pins <= 9:
        r = 3
    else:
        r = 4  # fine-pitch IC pins last
    return (r, f"{prefix}{num:04d}")


def build_board(board_id: str, pcb_path: Optional[str] = None) -> Board:
    cfg = load_cfg(board_id)
    root = ET.parse(BOARDS / board_id / "netlist.xml").getroot()
    overrides = (cfg.get("roles") or {})
    comps: Dict[str, Component] = {}
    for c in root.find("components"):
        dnp = any(pr.get("name") == "dnp" for pr in c.findall("property"))
        comps[c.get("ref")] = Component(ref=c.get("ref"), value=c.findtext("value") or "", footprint=c.findtext("footprint") or "", dnp=dnp)
    nets: List[Net] = []
    for n in root.find("nets"):
        name = n.get("name")
        nodes = [NetNode(ref=x.get("ref"), pin=x.get("pin")) for x in n.findall("node")]
        nets.append(Net(name=name, role=classify_net(name, overrides), nodes=nodes))
        for x in n.findall("node"):
            comp = comps.get(x.get("ref"))
            if comp is not None:
                comp.pins.append(Pin(pin=x.get("pin"), name=x.get("pinfunction") or x.get("pin"), net=name))
    roles = {n.name: n.role for n in nets}

    # positions + outline
    outline = Outline(w=50.0, h=50.0)
    if pcb_path:
        from .pcb_pos import read_pcb
        pcb = read_pcb(pcb_path)
        x0, y0 = pcb["outline"]["x0"], pcb["outline"]["y0"]
        outline = Outline(w=pcb["outline"]["w"], h=pcb["outline"]["h"])
        for ref, fp in pcb["footprints"].items():
            if ref in comps:
                c = comps[ref]
                c.x, c.y, c.rot = round(fp["x"] - x0, 3), round(fp["y"] - y0, 3), fp["rot"]
                for p in c.pins:
                    if p.pin in fp["pads"]:
                        px, py = fp["pads"][p.pin]
                        p.x, p.y = round(px - x0, 3), round(py - y0, 3)

    # rails / nominal voltages
    rails_cfg: Dict[str, dict] = cfg.get("rails") or {}

    def rail_v(net: str) -> float:
        if net in rails_cfg and "nominal_v" in rails_cfg[net]:
            return float(rails_cfg[net]["nominal_v"])
        m = re.match(r"^\+?(\d+)[V.](\d*)V?$", net.lstrip("/"))
        return float(f"{m.group(1)}.{m.group(2) or 0}") if m else 0.0

    # power tree: component with an IN-like pin and an OUT-like pin on two different rails
    edges: Dict[Tuple[str, str, str], PowerEdge] = {}
    for c in comps.values():
        ins = {p.net for p in c.pins if roles.get(p.net) == "power_rail" and IN_PIN_RE.search(p.name)}
        outs = {p.net for p in c.pins if roles.get(p.net) == "power_rail" and OUT_PIN_RE.search(p.name)}
        for i in ins:
            for o in outs:
                if i != o:
                    edges[(c.ref, i, o)] = PowerEdge(ref=c.ref, in_net=i, out_net=o, nominal_v=rail_v(o))
    for e in cfg.get("power_tree") or []:
        edges[(e["ref"], e["in_net"], e["out_net"])] = PowerEdge(ref=e["ref"], in_net=e["in_net"], out_net=e["out_net"], nominal_v=float(e.get("nominal_v", rail_v(e["out_net"]))))

    # test points: one per multi-node net, at the most probe-friendly pad
    tps: List[TestPoint] = []
    smoke = cfg.get("smoke_test") or {}
    ncount = {r: len(c.pins) for r, c in comps.items()}
    for n in nets:
        if len(n.nodes) < 2 and n.role != "ground":
            continue
        populated = [nd for nd in n.nodes if nd.ref in comps and not comps[nd.ref].dnp] or n.nodes
        best = sorted(populated, key=lambda nd: _tp_rank(nd.ref, ncount.get(nd.ref, 99)))[0]
        if n.role == "ground":
            kinds = ["r_pair"]
        elif n.role == "power_rail":
            kinds = ["v", "r_gnd", "r_pair"]
        elif n.role == "clock":
            kinds = ["freq", "v", "r_gnd", "r_pair", "logic"]
        else:
            kinds = ["v", "r_gnd", "r_pair", "logic"]
        if n.name == smoke.get("uart_tx"):
            kinds.append("uart")
        tps.append(TestPoint(tp=tp_name(n.name), net=n.name, ref=best.ref, pin=best.pin, kinds=kinds))
    sup = cfg.get("supply") or {}
    sup_net = sup.get("net", "VBUS")
    sup_tp = next((t for t in tps if t.net == sup_net), None)
    if sup_tp:
        tps.append(TestPoint(tp="SUPPLY", net=sup_net, ref=sup_tp.ref, pin=sup_tp.pin, kinds=["i_supply"]))
    usb_dp = next((n for n in nets if n.role == "usb" and re.search(r"(?i)d(p|\+)$", n.name) and any(nd.ref.startswith("J") for nd in n.nodes)), None)
    if usb_dp:
        nd = next(nd for nd in usb_dp.nodes if nd.ref.startswith("J"))
        tps.append(TestPoint(tp="USB_HOST", net=usb_dp.name, ref=nd.ref, pin=nd.pin, kinds=["usb_enum"]))

    board = Board(
        board_id=board_id,
        components=list(comps.values()),
        nets=nets,
        power_tree=list(edges.values()),
        nominal={},
        testpoints=tps,
        outline=outline,
    )
    # golden-board nominal readings come from the same behavioral model the fixture uses
    from .fixture import golden_nominal
    board.nominal = golden_nominal(board, cfg)
    return board


class BoardIndex:
    """Read-only lookups over a Board. Safe to hand to triage methods (no truth inside)."""

    def __init__(self, board: Board):
        self.board = board
        self.net = {n.name: n for n in board.nets}
        self.comp = {c.ref: c for c in board.components}
        self.tp = {t.tp: t for t in board.testpoints}
        self.tp_of_net: Dict[str, str] = {}
        for t in board.testpoints:
            if t.tp.startswith("TP_"):
                self.tp_of_net.setdefault(t.net, t.tp)
        self.pin_net: Dict[Tuple[str, str], str] = {}
        for c in board.components:
            for p in c.pins:
                self.pin_net[(c.ref, p.pin)] = p.net

    def role(self, net: str) -> str:
        return self.net[net].role if net in self.net else "other"

    def nets_with_role(self, role: str) -> List[str]:
        return [n.name for n in self.board.nets if n.role == role]

    @cached_property
    def ground(self) -> str:
        g = self.nets_with_role("ground")
        return g[0] if g else "GND"

    def is_ic(self, ref: str) -> bool:
        return ref.startswith("U") and not (ref in self.comp and self.comp[ref].dnp)

    def two_pin(self, ref: str) -> Optional[Tuple[str, str]]:
        c = self.comp.get(ref)
        if not c or c.dnp or len(c.pins) != 2:
            return None
        return c.pins[0].net, c.pins[1].net

    @cached_property
    def resistors(self) -> List[Tuple[str, str, str, float]]:
        out = []
        for c in self.board.components:
            if c.ref.startswith("R") and len(c.pins) == 2 and not c.dnp:
                v = parse_si(c.value)
                if v is not None:
                    out.append((c.ref, c.pins[0].net, c.pins[1].net, v))
        return out

    @cached_property
    def rails(self) -> List[str]:
        return self.nets_with_role("power_rail")

    @cached_property
    def rail_root(self) -> Optional[str]:
        outs = {e.out_net for e in self.board.power_tree}
        roots = [e.in_net for e in self.board.power_tree if e.in_net not in outs]
        return roots[0] if roots else (self.rails[0] if self.rails else None)

    def edge_into(self, rail: str):
        return next((e for e in self.board.power_tree if e.out_net == rail), None)

    def path_to(self, rail: str) -> List:
        """Edges from the root down to `rail` (root first)."""
        path, cur, seen = [], rail, set()
        while True:
            e = self.edge_into(cur)
            if not e or cur in seen:
                break
            seen.add(cur)
            path.append(e)
            cur = e.in_net
        return list(reversed(path))

    def descendants(self, rail: str) -> List[str]:
        out, frontier = [], [rail]
        while frontier:
            cur = frontier.pop()
            for e in self.board.power_tree:
                if e.in_net == cur and e.out_net not in out:
                    out.append(e.out_net)
                    frontier.append(e.out_net)
        return out

    def rail_order(self) -> List[str]:
        """Rails in power-up order: root, then breadth-first down the tree, then any orphan rails."""
        order = [self.rail_root] if self.rail_root else []
        i = 0
        while i < len(order):
            for e in self.board.power_tree:
                if e.in_net == order[i] and e.out_net not in order:
                    order.append(e.out_net)
            i += 1
        return order + [r for r in self.rails if r not in order]

    def package_pins_per_side(self, ref: str) -> Optional[int]:
        c = self.comp.get(ref)
        if not c:
            return None
        m = re.search(r"(?:QFN|DFN|QFP|LQFP|TQFP)-(\d+)", c.footprint)
        if not m:
            return None
        n = int(m.group(1))
        return n // 4 if "QFN" in c.footprint or "QFP" in c.footprint else n // 2

    def adjacent_pin_pairs(self, ref: str) -> List[Tuple[str, str]]:
        """(n, n+1) pairs on the same package side. Exposed pad and cross-corner pairs excluded."""
        side = self.package_pins_per_side(ref)
        c = self.comp.get(ref)
        if not side or not c:
            return []
        total = side * (4 if ("QFN" in c.footprint or "QFP" in c.footprint) else 2)
        nums = {int(p.pin) for p in c.pins if p.pin.isdigit() and int(p.pin) <= total}
        pairs = []
        for n in sorted(nums):
            if n + 1 in nums and (n % side) != 0:
                pairs.append((str(n), str(n + 1)))
        return pairs

    def neighbors(self, ref_or_net: str) -> Dict[str, List[str]]:
        if ref_or_net in self.comp:
            return {"nets": sorted({p.net for p in self.comp[ref_or_net].pins})}
        if ref_or_net in self.net:
            refs = sorted({nd.ref for nd in self.net[ref_or_net].nodes})
            nets = sorted({p.net for r in refs if r in self.comp and len(self.comp[r].pins) <= 4 for p in self.comp[r].pins} - {ref_or_net})
            return {"refs": refs, "nets_via_small_parts": nets}
        return {}

    def led_paths(self) -> List[Tuple[str, str, str]]:
        """(led_ref, anode_net, series_resistor_ref) for every LED with a resistor on its anode."""
        out = []
        for c in self.board.components:
            if not c.ref.startswith("D") or "LED" not in c.value.upper() or c.dnp:
                continue
            anode = next((p.net for p in c.pins if p.name.upper() in ("A", "ANODE")), None)
            r = next((r for r, a, b, _ in self.resistors if anode in (a, b)), None)
            if anode:
                out.append((c.ref, anode, r or ""))
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--board", default="rp2040_easyduino")
    b.add_argument("--pcb", default=None)
    args = ap.parse_args()
    if args.cmd == "build":
        board = build_board(args.board, args.pcb)
        out = BOARDS / args.board / "board.json"
        out.write_text(json.dumps(board.model_dump(), indent=1))
        print(f"[board] {args.board}: {len(board.components)} components, {len(board.nets)} nets, "
              f"{len(board.power_tree)} power edges, {len(board.testpoints)} test points, outline {board.outline.w}x{board.outline.h} mm -> {out}")
        for e in board.power_tree:
            print(f"[board]   power: {e.in_net} --{e.ref}--> {e.out_net} ({e.nominal_v} V)")


if __name__ == "__main__":
    main()
