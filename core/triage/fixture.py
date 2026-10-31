"""Behavioral bring-up fixture: answers bench measurements for a (possibly faulted) board.

This is a rule-based behavioral model of a first-article PCBA on the bench, NOT a SPICE simulation.
It knows: a power tree with diode drops / LDO dropout, a current-limited bench supply, DMM R-to-GND
readings (rail golden values, IC ESD paths, resistors one hop away), a crystal oscillator, the RP2040
boot chain (flash XIP -> app, flash fail -> USB BOOTSEL ROM, no XOSC -> hang), a UART banner, an LED
and USB enumeration. Every analog reading gets seeded ~1% Gaussian noise (5 ppm on frequency).
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .schemas import OPEN_CIRCUIT_OHM, Board, FaultCase, Measurement, Nominal

ESD_OHM = 6.0e5          # unpowered DMM reading through one IC pin's ESD structure
CLOCK_TOL_PPM = 30.0
UNITS = {"v": "V", "r_gnd": "ohm", "r_pair": "ohm", "freq": "Hz", "i_supply": "mA", "logic": "", "uart": "", "usb_enum": ""}


def _parallel(vals: List[float]) -> float:
    vals = [v for v in vals if v is not None and v < OPEN_CIRCUIT_OHM]
    if not vals:
        return OPEN_CIRCUIT_OHM
    if any(v <= 0 for v in vals):
        return 0.0
    return 1.0 / sum(1.0 / v for v in vals)


def _edge_drop(ref: str) -> float:
    return 0.30 if ref.startswith("D") else 0.25  # Schottky drop / LDO dropout (behavioral)


@dataclass
class BoardState:
    rail_v: Dict[str, float]
    i_supply_ma: float
    osc_hz: float
    boot: str                      # app | bootrom | hung | dead
    uart: str
    usb: str                       # app | RP2 Boot | enum_error | none
    logic: Dict[str, str]
    sig_v: Dict[str, float]
    shorts: Dict[str, float] = field(default_factory=dict)
    open_pins: Set[Tuple[str, str]] = field(default_factory=set)
    bridges: List[Tuple[str, str, float]] = field(default_factory=list)
    r_override: Dict[str, float] = field(default_factory=dict)


class Model:
    """Static electrical helpers for one board + config."""

    def __init__(self, board: Board, cfg: dict):
        from .netlist import BoardIndex  # local import: netlist imports this module
        self.board, self.cfg = board, cfg
        self.idx = BoardIndex(board)
        self.rails_cfg: Dict[str, dict] = cfg.get("rails") or {}
        self.smoke: Dict[str, str] = cfg.get("smoke_test") or {}
        self.supply = cfg.get("supply") or {"net": self.idx.rail_root, "v": 5.0, "i_limit_ma": 150}
        self.core_ma: Dict[str, float] = cfg.get("core_ma") or {"app": 22.0, "bootrom": 14.0, "hung": 6.0}
        self.clock_hz = float((cfg.get("clock") or {}).get("freq_hz", 12e6))
        self.mcu = cfg.get("mcu") or "U1"

    # ---- topology helpers ----
    def ic_pins(self, net: str, open_pins: Set[Tuple[str, str]]) -> int:
        n = self.idx.net.get(net)
        if not n:
            return 0
        return sum(1 for nd in n.nodes if self.idx.is_ic(nd.ref) and (nd.ref, nd.pin) not in open_pins)

    def rail_nominal(self, rail: str) -> float:
        if rail in self.rails_cfg and "nominal_v" in self.rails_cfg[rail]:
            return float(self.rails_cfg[rail]["nominal_v"])
        e = self.idx.edge_into(rail)
        return e.nominal_v if e else float(self.supply.get("v", 5.0))

    def resistor_value(self, ref: str, nominal: float, st: Optional[BoardState]) -> float:
        if st and ref in st.r_override:
            return st.r_override[ref]
        return nominal

    # ---- DMM readings (unpowered) ----
    def _base_r(self, net: str, st: Optional[BoardState]) -> float:
        g = self.idx.ground
        if net == g:
            return 0.0
        if st and net in st.shorts:
            return st.shorts[net]
        if self.idx.role(net) == "power_rail":
            return float(self.rails_cfg.get(net, {}).get("r_gnd", 1e4))
        opens = st.open_pins if st else set()
        paths: List[float] = [ESD_OHM] * self.ic_pins(net, opens)
        for ref, a, b, r in self.idx.resistors:
            if net not in (a, b) or a == b:
                continue
            other = b if a == net else a
            rv = self.resistor_value(ref, r, st)
            if other == g:
                paths.append(rv)
            elif self.idx.role(other) == "power_rail":
                paths.append(rv + float(self.rails_cfg.get(other, {}).get("r_gnd", 1e4)))
            else:
                m = self.ic_pins(other, opens)
                if m:
                    paths.append(rv + ESD_OHM / m)
        return _parallel(paths)

    def bridge_group(self, net: str, st: Optional[BoardState]) -> Tuple[Set[str], float]:
        group, best = {net}, OPEN_CIRCUIT_OHM
        if not st:
            return group, best
        changed = True
        while changed:
            changed = False
            for a, b, r in st.bridges:
                if (a in group) != (b in group):
                    group |= {a, b}
                    best = min(best, r)
                    changed = True
        return group, best

    def r_gnd(self, net: str, st: Optional[BoardState]) -> float:
        group, bridge_r = self.bridge_group(net, st)
        if self.idx.ground in group and net != self.idx.ground:
            return bridge_r
        return _parallel([self._base_r(n, st) for n in group])

    def r_pair(self, a: str, b: str, st: Optional[BoardState]) -> float:
        if a == b:
            return 0.02
        group, bridge_r = self.bridge_group(a, st)
        if b in group:
            return bridge_r
        g = self.idx.ground
        if b == g:
            return self.r_gnd(a, st)
        if a == g:
            return self.r_gnd(b, st)
        direct = [self.resistor_value(ref, r, st) for ref, x, y, r in self.idx.resistors if {x, y} == {a, b}]
        return _parallel(direct) if direct else OPEN_CIRCUIT_OHM


def golden_nominal(board: Board, cfg: dict) -> Dict[str, Nominal]:
    m = Model(board, cfg)
    out: Dict[str, Nominal] = {}
    for n in board.nets:
        if len(n.nodes) < 2 or n.role == "ground":
            continue
        r = m.r_gnd(n.name, None)
        nom = Nominal(r_gnd=None if r >= OPEN_CIRCUIT_OHM else round(r, 1))
        if n.role == "power_rail":
            nom.v = m.rail_nominal(n.name)
            nom.r_gnd_min = round(0.5 * r, 1)
        if n.role == "clock":
            nom.freq_hz = m.clock_hz
        out[n.name] = nom
    return out


def simulate(board: Board, cfg: dict, case: Optional[FaultCase]) -> BoardState:
    m = Model(board, cfg)
    idx = m.idx
    p = case.params if case else {}
    cls = case.cls if case else None
    st = BoardState(rail_v={}, i_supply_ma=0.0, osc_hz=0.0, boot="dead", uart="", usb="none", logic={}, sig_v={})
    i_lim = float(m.supply.get("i_limit_ma", 150))
    sup_v = float(m.supply.get("v", 5.0))
    root = idx.rail_root

    osc_ok, ppm, flash_ok, uart_ok = True, 0.0, True, True
    led_state = "toggling"
    usb_path = "ok"
    stuck: Dict[str, Tuple[str, float]] = {}   # net -> ("low"|"high", volts)
    follows: Set[str] = set()                  # nets bridged to a toggling output
    broken_edges: Set[Tuple[str, str]] = set()
    cc_mode, overvolt, run_low = False, False, False
    contention_ma = 0.0
    sag_rail: Optional[str] = None
    uart_net, led_gpio, led_node = m.smoke.get("uart_tx"), m.smoke.get("led_gpio"), m.smoke.get("led_node")
    driven_outputs = {x for x in (uart_net, led_gpio) if x}

    def is_dp(net: str, pin_name: str = "") -> bool:
        return bool(re.search(r"(?i)(dp|d\+|_p)$", pin_name or "") or re.search(r"(?i)(dp|d\+)$", net))

    def signal_effect(net: str, mode: str) -> None:
        """mode: low | high | pair"""
        nonlocal osc_ok, flash_ok, uart_ok, led_state, usb_path, run_low
        role = idx.role(net)
        if role == "clock":
            osc_ok = False
        elif role == "qspi":
            flash_ok = False
        elif role == "usb":
            if mode == "low" and is_dp(net):
                usb_path = "none"
            elif usb_path == "ok":
                usb_path = "enum_error"
        elif role == "reset" and mode == "low":
            run_low = True
        if net == uart_net and mode in ("low", "high"):
            uart_ok = False
        if net in (led_gpio, led_node) and mode in ("low", "high"):
            led_state = mode

    # ---------------- apply the fault ----------------
    if cls == "RAIL_SHORT":
        rail, r = str(p["rail"]), float(p["r_short"])
        st.shorts[rail] = r
        cc_mode = True
    elif cls == "RAIL_OPEN":
        broken_edges.add((str(p["in_net"]), str(p["out_net"])))
    elif cls == "WRONG_VALUE":
        ref, actual = str(p["ref"]), float(p["actual"])
        if ref.startswith("R"):
            st.r_override[ref] = actual
            nets = idx.two_pin(ref) or ("", "")
            if any(idx.role(n) == "usb" for n in nets):
                usb_path = "enum_error"
            if led_node in nets:
                led_state = "toggling_dim"
        elif ref.startswith("C"):
            ppm = -float(p.get("pull_ppm", 200.0))
    elif cls == "CLOCK_FAIL":
        osc_ok = False
    elif cls == "PIN_BRIDGE":
        a, b = str(p["net_a"]), str(p["net_b"])
        st.bridges.append((a, b, float(p["r_bridge"])))
        ra, rb = idx.role(a), idx.role(b)
        if "ground" in (ra, rb):
            sig = b if ra == "ground" else a
            stuck[sig] = ("low", 0.0)
            signal_effect(sig, "low")
            if sig in driven_outputs:
                contention_ma += 30.0
                sag_rail = next((e.out_net for e in board.power_tree if m.rail_nominal(e.out_net) > 3.0 and m.rail_nominal(e.out_net) < 4.0), None)
        elif ra == "power_rail" and rb == "power_rail":
            overvolt = True
            cc_mode = True
        elif "power_rail" in (ra, rb):
            rail, sig = (a, b) if ra == "power_rail" else (b, a)
            stuck[sig] = ("high", m.rail_nominal(rail))
            signal_effect(sig, "high")
            if sig in driven_outputs:
                contention_ma += 30.0
                sag_rail = rail
        else:
            for n in (a, b):
                signal_effect(n, "pair")
            if led_gpio in (a, b):
                follows |= {a, b} - {led_gpio}
            if uart_net in (a, b):
                follows |= {a, b} - {uart_net}
    elif cls == "SIGNAL_OPEN":
        ref, pin, net = str(p["ref"]), str(p["pin"]), str(p["net"])
        st.open_pins.add((ref, pin))
        pin_name = next((pp.name for pp in idx.comp[ref].pins if pp.pin == pin), "")
        role = idx.role(net)
        if role == "clock":
            osc_ok = False
        elif role == "qspi":
            flash_ok = False
        elif role == "usb":
            usb_path = "none" if is_dp(net, pin_name) else "enum_error"
        if net == uart_net:
            uart_ok = False
        if net in (led_gpio, led_node):
            led_state = "low"

    # ---------------- power tree ----------------
    order = idx.rail_order()
    V: Dict[str, float] = {r: 0.0 for r in idx.rails}
    if cls == "RAIL_SHORT":
        rail = str(p["rail"])
        V[rail] = i_lim / 1000.0 * st.shorts[rail]
        for e in reversed(idx.path_to(rail)):
            V[e.in_net] = V[e.out_net] + _edge_drop(e.ref)
        fixed = {rail} | {e.in_net for e in idx.path_to(rail)}
    else:
        if root:
            V[root] = sup_v
        fixed = {root} if root else set()
    for r in order:
        if r in fixed:
            continue
        e = idx.edge_into(r)
        if not e:
            continue
        if (e.in_net, e.out_net) in broken_edges:
            V[r] = 0.0
            continue
        vin = V.get(e.in_net, 0.0)
        out = vin - _edge_drop(e.ref) if e.ref.startswith("D") else min(m.rail_nominal(r), vin - _edge_drop(e.ref))
        V[r] = max(0.0, out)
    if overvolt:  # power-power bridge: lower rail dragged toward the higher one
        a, b = str(p["net_a"]), str(p["net_b"])
        hi, lo = (a, b) if m.rail_nominal(a) >= m.rail_nominal(b) else (b, a)
        V[lo] = 0.95 * V[hi]
    if sag_rail and sag_rail in V:
        V[sag_rail] *= 0.97
    st.rail_v = V

    powered = all(V[r] >= 0.85 * m.rail_nominal(r) for r in idx.rails if m.rail_nominal(r) > 0) and not overvolt
    io_rail_ok = any(V[r] >= 0.85 * m.rail_nominal(r) and 3.0 <= m.rail_nominal(r) <= 3.6 for r in idx.rails)

    # ---------------- boot chain ----------------
    if powered and not run_low:
        st.osc_hz = m.clock_hz * (1 + ppm * 1e-6) if osc_ok else 0.0
        st.boot = ("app" if osc_ok else "hung") if flash_ok else "bootrom"
    else:
        st.osc_hz, st.boot = 0.0, "dead"
    st.uart = str(m.smoke.get("banner", "BOOT OK")) if (st.boot == "app" and uart_ok) else ""
    if st.boot in ("app", "bootrom") and st.osc_hz > 0:
        st.usb = {"ok": "app" if st.boot == "app" else "RP2 Boot", "enum_error": "enum_error"}.get(usb_path, "none")
    else:
        st.usb = "none"

    # ---------------- supply current ----------------
    if cc_mode:
        st.i_supply_ma = i_lim
    else:
        i = sum(float(m.rails_cfg.get(r, {}).get("load_ma", 0.0)) for r in idx.rails if V[r] >= 0.85 * m.rail_nominal(r))
        if st.boot in m.core_ma:
            i += float(m.core_ma[st.boot])
        elif powered and run_low:
            i += 1.0
        if st.boot == "app":
            i += {"toggling": 0.75, "toggling_dim": 0.08, "high": 1.5}.get(led_state, 0.0) + contention_ma
        st.i_supply_ma = i

    # ---------------- logic + DC levels on signal nets ----------------
    for n in board.nets:
        name, role = n.name, n.role
        if role in ("power_rail", "ground") or len(n.nodes) < 2:
            continue
        mcu_open = any((nd.ref, nd.pin) in st.open_pins for nd in n.nodes if nd.ref == m.mcu)
        pulled_high = any(idx.role(o) == "power_rail" and V.get(o, 0) > 0.5 for ref, a, b, _ in idx.resistors
                          if name in (a, b) for o in ((b if a == name else a),))
        idle = "high" if pulled_high else "low"
        if not io_rail_ok:
            lv = "low"
        elif name in stuck:
            lv = stuck[name][0]
        elif name in follows and st.boot == "app":
            lv = "toggling"
        elif mcu_open:
            lv = idle if pulled_high else "floating"
        elif role == "clock":
            lv = "toggling" if st.osc_hz > 0 else "floating"
        elif role == "qspi":
            lv = "toggling" if st.boot == "app" else idle
        elif name == led_node:
            lv = led_state if st.boot == "app" else "low"
        elif name == led_gpio:
            lv = ("toggling" if led_state in ("toggling", "toggling_dim") else led_state) if st.boot == "app" else "low"
        elif name == uart_net:
            lv = "high" if st.boot == "app" else "low"
        elif role == "usb":
            lv = "high" if (is_dp(name) and st.usb in ("app", "RP2 Boot", "enum_error")) else "low"
        elif role == "reset":
            lv = "high" if not run_low else "low"
        else:
            lv = idle
        st.logic[name] = lv
        if name in stuck:
            st.sig_v[name] = stuck[name][1] if stuck[name][0] == "high" else 0.0
        elif role == "clock" and io_rail_ok:
            st.sig_v[name] = 0.9 if st.osc_hz > 0 else (0.35 if mcu_open else 1.6)
        else:
            st.sig_v[name] = {"high": 3.3, "low": 0.0, "floating": 0.4, "toggling": 1.65, "toggling_dim": 1.62}.get(lv, 0.0)
    return st


def _rng(seed: int, *key) -> random.Random:
    h = hashlib.sha256(("|".join(str(k) for k in (seed,) + key)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


class Fixture:
    """One board on the bench. Owns the hidden case; exposes only measurements."""

    def __init__(self, board: Board, cfg: dict, case: Optional[FaultCase]):
        self._model = Model(board, cfg)
        self._case = case
        self._state = simulate(board, cfg, case)
        self._seed = case.seed if case else 0

    @property
    def board(self) -> Board:
        return self._model.board

    def measure(self, tp: str, kind: str, tp2: Optional[str] = None, stage: str = "PROBE") -> Measurement:
        idx, st, m = self._model.idx, self._state, self._model
        if tp not in idx.tp:
            raise ValueError(f"unknown test point {tp!r}")
        t = idx.tp[tp]
        if kind not in t.kinds and not (kind == "r_pair" and tp2):
            raise ValueError(f"{tp} does not support {kind!r} (supports {t.kinds})")
        rng = _rng(self._seed, tp, kind, tp2 or "")
        net = t.net
        val: object
        if kind == "r_gnd":
            val = self._noisy_r(m.r_gnd(net, st), rng)
        elif kind == "r_pair":
            if not tp2 or tp2 not in idx.tp:
                raise ValueError(f"r_pair needs a valid tp2 (got {tp2!r})")
            val = self._noisy_r(m.r_pair(net, idx.tp[tp2].net, st), rng)
        elif kind == "v":
            true_v = st.rail_v.get(net) if idx.role(net) == "power_rail" else st.sig_v.get(net, 0.0)
            val = round(max(0.0, true_v + rng.gauss(0, 0.01 * abs(true_v)) + rng.gauss(0, 0.002)), 4)
        elif kind == "freq":
            f = st.osc_hz if idx.role(net) == "clock" else 0.0
            val = 0.0 if f <= 0 else float(round(f * (1 + rng.gauss(0, 5e-6))))
        elif kind == "i_supply":
            i = st.i_supply_ma
            val = round(max(0.0, i + rng.gauss(0, 0.01 * i) + rng.gauss(0, 0.05)), 3)
            if i >= float(m.supply.get("i_limit_ma", 150)):
                val = round(float(m.supply.get("i_limit_ma", 150)) - abs(rng.gauss(0, 0.2)), 3)
        elif kind == "logic":
            val = st.logic.get(net, "low")
        elif kind == "uart":
            val = st.uart
        elif kind == "usb_enum":
            val = st.usb
        else:
            raise ValueError(f"unsupported kind {kind!r}")
        return Measurement(tp=tp, tp2=tp2, kind=kind, value=val, unit=UNITS[kind], stage=stage)

    @staticmethod
    def _noisy_r(r: float, rng: random.Random) -> float:
        if r >= OPEN_CIRCUIT_OHM:
            return OPEN_CIRCUIT_OHM
        noisy = r * (1 + rng.gauss(0, 0.01)) + abs(rng.gauss(0, 0.005))
        return float(f"{max(0.0, noisy):.4g}")

    def panel(self) -> List[Measurement]:
        """Standard bring-up panel S1-S5, S7, S8 (S0/S6 informational, not simulated)."""
        idx, m = self._model.idx, self._model
        out: List[Measurement] = []
        rails = [r for r in idx.rail_order() if r in idx.tp_of_net]
        for r in rails:
            out.append(self.measure(idx.tp_of_net[r], "r_gnd", stage="S1"))
        if "SUPPLY" in idx.tp:
            out.append(self.measure("SUPPLY", "i_supply", stage="S2"))
        for r in rails:
            out.append(self.measure(idx.tp_of_net[r], "v", stage="S3"))
        for n in idx.nets_with_role("clock"):
            if any(nd.ref == m.mcu for nd in idx.net[n].nodes) and n in idx.tp_of_net:
                out.append(self.measure(idx.tp_of_net[n], "freq", stage="S4"))
        for n in idx.nets_with_role("reset"):
            if n in idx.tp_of_net:
                out.append(self.measure(idx.tp_of_net[n], "v", stage="S5"))
        sclk = next((n for n in idx.nets_with_role("qspi") if re.search(r"(?i)clk", n)), None)
        if sclk and sclk in idx.tp_of_net:
            out.append(self.measure(idx.tp_of_net[sclk], "logic", stage="S5"))
        smoke = m.smoke
        if smoke.get("uart_tx") in idx.tp_of_net:
            out.append(self.measure(idx.tp_of_net[smoke["uart_tx"]], "uart", stage="S7"))
        for key in ("led_gpio", "led_node"):
            if smoke.get(key) in idx.tp_of_net:
                out.append(self.measure(idx.tp_of_net[smoke[key]], "logic", stage="S7"))
        if "USB_HOST" in idx.tp:
            out.append(self.measure("USB_HOST", "usb_enum", stage="S8"))
        return out


class BudgetExhausted(RuntimeError):
    pass


class ProbeSession:
    """What a triage method gets: probe() with the budget enforced in code. No access to the fault case."""

    def __init__(self, fixture: "Fixture", budget: int):
        self._measure = fixture.measure
        self.budget = budget
        self.probes: List[Measurement] = []
        self.bench_seconds = 0.0
        # The Agents SDK runs sync tools via asyncio.to_thread, so parallel probe calls in one model turn
        # arrive on different threads. Without the lock they all pass the budget check (DECISIONS D13).
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        return self.budget - len(self.probes)

    def probe(self, tp: str, kind: str, tp2: Optional[str] = None) -> Measurement:
        from .schemas import PROBE_COST_S
        with self._lock:
            if self.remaining <= 0:
                raise BudgetExhausted(f"probe budget of {self.budget} used up")
            m = self._measure(tp, kind, tp2 or None, stage="PROBE")  # invalid probes raise and do not count
            self.probes.append(m)
            self.bench_seconds += PROBE_COST_S.get(kind, 20)
            return m
