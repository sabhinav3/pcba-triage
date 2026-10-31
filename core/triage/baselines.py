"""Two non-LLM triage methods.

none  : static prior. No probes, ignores measurements. rails -> clock -> qspi -> usb -> gpio, ties by net degree.
rules : deterministic bring-up decision tree on the standard panel, fixed probe order per branch, K <= budget.

Both only see an Observation (public board + measurements) and a ProbeSession. Generic over boards: they use
net roles, the power tree, LED/UART test points and golden r_gnd values from the public board, never refdes.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .fixture import BudgetExhausted, ProbeSession
from .netlist import BoardIndex
from .schemas import OPEN_CIRCUIT_OHM, Hypothesis, Measurement, Observation, ProbeRequest

ROLE_PRIOR = ["power_rail", "clock", "qspi", "usb", "gpio"]
ROLE_CLASS = {"power_rail": "RAIL_SHORT", "clock": "CLOCK_FAIL", "qspi": "SIGNAL_OPEN", "usb": "SIGNAL_OPEN", "gpio": "SIGNAL_OPEN"}


def _degree(idx: BoardIndex, net: str) -> int:
    return len(idx.net[net].nodes)


def prior_nets(idx: BoardIndex) -> List[str]:
    out: List[str] = []
    for role in ROLE_PRIOR:
        nets = [n for n in idx.nets_with_role(role) if _degree(idx, n) >= 2]
        out += sorted(nets, key=lambda n: (-_degree(idx, n), n))
    return out


def crystal_refs(idx: BoardIndex) -> List[str]:
    return [c.ref for c in idx.board.components if re.match(r"^(Y|X)\d", c.ref)]


def mcu_ref(idx: BoardIndex) -> str:
    ics = [c for c in idx.board.components if idx.is_ic(c.ref)]
    return max(ics, key=lambda c: len(c.pins)).ref if ics else ""


def ic_on_net(idx: BoardIndex, net: str) -> List[str]:
    refs = [nd.ref for nd in idx.net[net].nodes if idx.is_ic(nd.ref)] if net in idx.net else []
    return sorted(set(refs), key=lambda r: -len(idx.comp[r].pins))


def caps_on_rail(idx: BoardIndex, rail: str) -> List[str]:
    g = idx.ground
    caps = [c.ref for c in idx.board.components if c.ref.startswith("C") and idx.two_pin(c.ref) and set(idx.two_pin(c.ref)) == {rail, g}]
    return sorted(caps, key=lambda r: int(re.sub(r"\D", "", r) or 0))


def static_hypotheses(idx: BoardIndex, exclude: Optional[set] = None, start_rank: int = 1, k: int = 5) -> List[Hypothesis]:
    out, xs = [], crystal_refs(idx)
    for net in prior_nets(idx):
        if exclude and net in exclude:
            continue
        role = idx.role(net)
        refs = caps_on_rail(idx, net)[:3] if role == "power_rail" else (xs[:1] if role == "clock" else ic_on_net(idx, net)[:1])
        out.append(Hypothesis(rank=start_rank + len(out), cls=ROLE_CLASS[role], nets=[net], refs=refs,
                              confidence=round(0.3 / (len(out) + 1), 3), evidence=["static prior: role order, then net degree"]))
        if len(out) >= k:
            break
    return out


def triage_none(obs: Observation, session: ProbeSession) -> List[Hypothesis]:
    return static_hypotheses(BoardIndex(obs.board_public))


class _Rules:
    def __init__(self, obs: Observation, session: ProbeSession):
        self.obs, self.s = obs, session
        self.idx = BoardIndex(obs.board_public)
        self.nom = obs.board_public.nominal
        self.M: Dict[Tuple[str, str], object] = {(m.tp, m.kind): m.value for m in obs.measurements}
        self.hyps: List[Hypothesis] = []
        self.mcu = mcu_ref(self.idx)
        vr = [(abs((self.nom[r].v or 0) - 3.3), r) for r in self.idx.rails if r in self.nom]
        self.io_rail = min(vr)[1] if vr else ""

    # -- helpers --
    def tp(self, net: str) -> Optional[str]:
        return self.idx.tp_of_net.get(net)

    def val(self, net: str, kind: str):
        t = self.tp(net)
        return self.M.get((t, kind)) if t else None

    def probe(self, net: str, kind: str, net2: Optional[str] = None) -> Optional[Measurement]:
        t, t2 = self.tp(net), (self.tp(net2) if net2 else None)
        if not t or (net2 and not t2) or self.s.remaining <= 0:
            return None
        try:
            m = self.s.probe(t, kind, t2)
        except (BudgetExhausted, ValueError):
            return None
        self.M[(t if not t2 else f"{t}|{t2}", kind)] = m.value
        return m

    def add(self, cls: str, nets: List[str], refs: List[str], conf: float, *evidence: str, nxt: Optional[ProbeRequest] = None) -> None:
        key = (cls, tuple(sorted(nets)))
        if any((h.cls, tuple(sorted(h.nets))) == key for h in self.hyps):
            return
        self.hyps.append(Hypothesis(rank=len(self.hyps) + 1, cls=cls, nets=nets, refs=refs, confidence=round(conf, 3),
                                    evidence=list(evidence), next_probe=nxt))

    def rgnd_ratio(self, net: str, r: float) -> float:
        g = self.nom.get(net)
        golden = g.r_gnd if g and g.r_gnd else OPEN_CIRCUIT_OHM
        return r / golden if golden else 1.0

    def finish(self) -> List[Hypothesis]:
        seen = {n for h in self.hyps for n in h.nets}
        if len(self.hyps) < 5:
            for h in static_hypotheses(self.idx, exclude=seen, k=5 - len(self.hyps)):
                self.add(h.cls, h.nets, h.refs, min(h.confidence, 0.05), "fallback: static prior")
        hs = sorted(self.hyps, key=lambda h: -h.confidence)[:5]
        for i, h in enumerate(hs):
            h.rank = i + 1
        return hs

    # -- decision tree --
    def run(self) -> List[Hypothesis]:
        idx, nom = self.idx, self.nom
        rails = [r for r in idx.rail_order() if self.tp(r)]
        rv = {r: self.val(r, "v") for r in rails}
        rr = {r: self.val(r, "r_gnd") for r in rails}
        i_sup = self.M.get(("SUPPLY", "i_supply"))

        # 1. hard short on a rail
        shorted = sorted([(float(rr[r]), r) for r in rails if isinstance(rr[r], float) and rr[r] < 10.0])
        if shorted:
            r_ohm, rail = shorted[0]
            self.add("RAIL_SHORT", [rail], caps_on_rail(idx, rail)[:3], 0.9,
                     f"S1 r_gnd({rail})={r_ohm:.3g} ohm << golden {nom[rail].r_gnd}", f"S2 supply {i_sup} mA (at limit)")
            e = idx.edge_into(rail)
            if e:
                self.add("RAIL_OPEN", [e.in_net, e.out_net], [e.ref], 0.03, "alternative: regulator damage after short")
            return self.finish()

        # 2. supply at limit, no short: look for an over-voltage rail (power-power bridge)
        over = [r for r in rails if isinstance(rv[r], float) and nom[r].v and rv[r] > 1.2 * nom[r].v]
        if over:
            lo = over[0]
            e = idx.edge_into(lo)
            hi = e.in_net if e else rails[0]
            self.add("PIN_BRIDGE", [lo, hi], [self.mcu], 0.7, f"S3 {lo}={rv[lo]} V above nominal {nom[lo].v} V", f"S2 {i_sup} mA")
            self.add("RAIL_OPEN", [hi, lo], [e.ref] if e else [], 0.1, "regulator fault")
            return self.finish()

        # 3. first dead rail whose parent is alive -> open regulator path
        for r in rails:
            if not isinstance(rv[r], float) or not nom[r].v or rv[r] >= 0.5 * nom[r].v:
                continue
            e = idx.edge_into(r)
            parent_ok = (e is None) or (isinstance(rv.get(e.in_net), float) and nom[e.in_net].v and rv[e.in_net] >= 0.85 * nom[e.in_net].v)
            if parent_ok and e:
                self.add("RAIL_OPEN", [e.in_net, e.out_net], [e.ref], 0.85,
                         f"S3 {r}={rv[r]} V (nominal {nom[r].v}) while {e.in_net} ok", f"S1 r_gnd({r}) normal", f"S2 {i_sup} mA low")
                self.add("RAIL_SHORT", [r], caps_on_rail(idx, r)[:3], 0.05, "soft short not seen at S1")
                return self.finish()

        # 4. clock
        clocks = [n for n in idx.nets_with_role("clock") if self.val(n, "freq") is not None]
        freqs = {n: self.val(n, "freq") for n in clocks}
        xtal = crystal_refs(idx)
        xtal_nets = sorted({p.net for x in xtal for p in idx.comp[x].pins if idx.role(p.net) == "clock"})
        if clocks and any(f == 0.0 for f in freqs.values()):
            xin = next((n for n in clocks if re.search(r"(?i)xin", n)), clocks[0])
            xout = next((n for n in clocks if n != xin), None)
            r_in = self.probe(xin, "r_gnd")
            r_out = self.probe(xout, "r_gnd") if xout else None
            pair = self.probe(xin, "r_pair", xout) if xout else None
            found = False
            for net, m in ((xin, r_in), (xout, r_out)):
                if not m:
                    continue
                r = float(m.value)
                if r < 10:
                    self.add("PIN_BRIDGE", [net], [self.mcu], 0.8, f"probe r_gnd({net})={r:.3g} ohm: pin shorted to GND")
                    found = True
                elif r >= OPEN_CIRCUIT_OHM or self.rgnd_ratio(net, r) > 1.6:
                    self.add("SIGNAL_OPEN", [net], [self.mcu], 0.75, f"probe r_gnd({net})={r:.3g}: MCU pin not connected")
                    found = True
            if pair and float(pair.value) < 10:
                self.add("PIN_BRIDGE", [xin, xout], [self.mcu], 0.85, f"probe r_pair({xin},{xout})={pair.value} ohm")
                found = True
            self.add("CLOCK_FAIL", xtal_nets or [xin], xtal[:1], 0.6 if not found else 0.15,
                     "S4 no oscillation at XIN/XOUT, rails OK", "S7/S8 no banner, no USB")
            return self.finish()
        if clocks and all(isinstance(f, float) and f > 0 for f in freqs.values()):
            f0 = (nom[clocks[0]].freq_hz or 0) if clocks[0] in nom else 0
            ppm = max(abs(f / f0 - 1) * 1e6 for f in freqs.values()) if f0 else 0
            if ppm > 50:
                load_caps = [c.ref for c in idx.board.components if c.ref.startswith("C") and idx.two_pin(c.ref)
                             and any(idx.role(n) == "clock" for n in idx.two_pin(c.ref))]
                self.add("WRONG_VALUE", xtal_nets, load_caps[:2], 0.75, f"S4 frequency off by {ppm:.0f} ppm (> 50 ppm): load capacitance")
                self.add("CLOCK_FAIL", xtal_nets, xtal[:1], 0.15, "wrong crystal / damaged crystal")

        # 5. flash boot path
        usb = self.M.get(("USB_HOST", "usb_enum"))
        qspi = [n for n in idx.nets_with_role("qspi") if self.tp(n)]
        sclk = next((n for n in qspi if re.search(r"(?i)clk", n)), None)
        if usb == "RP2 Boot" or (sclk and self.val(sclk, "logic") not in (None, "toggling") and usb != "app"):
            if sclk and self.val(sclk, "logic") == "floating":
                self.add("SIGNAL_OPEN", [sclk], [self.mcu], 0.8, f"S5 {sclk} floating: MCU not driving the flash clock")
            # fixed order: data/clock nets whose golden r_gnd is ESD-dominated (pull-up nets are insensitive)
            sensitive = [n for n in qspi if nom.get(n) and nom[n].r_gnd and nom[n].r_gnd > 1e5]
            low_nets = []
            for n in sensitive:
                if self.s.remaining <= 0:
                    break
                m = self.probe(n, "r_gnd")
                if not m:
                    continue
                ratio = self.rgnd_ratio(n, float(m.value))
                if ratio > 1.6:
                    self.add("SIGNAL_OPEN", [n], [self.mcu], 0.7, f"probe r_gnd({n}) {ratio:.2f}x golden: one IC pin missing")
                elif ratio < 0.7:
                    low_nets.append(n)
            if len(low_nets) >= 2:
                self.add("PIN_BRIDGE", low_nets[:2], [self.mcu], 0.75, f"probe r_gnd low on {low_nets[:2]}: nets joined")
            elif low_nets:
                self.add("PIN_BRIDGE", low_nets, [self.mcu], 0.55, f"probe r_gnd({low_nets[0]}) low: bridged to a neighbour")
            unprobed = [n for n in qspi if not any(n in h.nets for h in self.hyps)]
            for n in unprobed[:2]:
                self.add("SIGNAL_OPEN", [n], ic_on_net(idx, n)[:1], 0.1, "S8 RP2 Boot: flash not readable; net not yet checked")
            return self.finish()

        smoke_uart = next((t for t in idx.board.testpoints if "uart" in t.kinds), None)
        led_paths = idx.led_paths()
        banner = self.M.get((smoke_uart.tp, "uart")) if smoke_uart else None

        # 6. LED path
        for led, anode, res in led_paths:
            node_logic = self.val(anode, "logic")
            gpio_net = next((n for n in (idx.two_pin(res) or ()) if n != anode), None) if res else None
            if node_logic == "toggling_dim":
                self.add("WRONG_VALUE", [gpio_net, anode] if gpio_net else [anode], [res], 0.85,
                         "S7 LED blinks but dim: series resistor value too high")
            elif node_logic in ("low", "high", "floating") and banner:
                tgt = gpio_net or anode
                m = self.probe(tgt, "r_gnd")
                if m and float(m.value) < 10:
                    self.add("PIN_BRIDGE", [tgt], [self.mcu], 0.75, f"probe r_gnd({tgt}) {m.value} ohm: GPIO pin shorted to GND")
                elif m and (float(m.value) >= OPEN_CIRCUIT_OHM or self.rgnd_ratio(tgt, float(m.value)) > 1.6):
                    self.add("SIGNAL_OPEN", [tgt], [self.mcu], 0.8, f"probe r_gnd({tgt}) open: MCU pin lifted")
                elif node_logic == "high":
                    self.add("PIN_BRIDGE", [tgt, self.io_rail], [self.mcu], 0.6, "LED stuck on: GPIO tied to supply")
                else:
                    self.add("SIGNAL_OPEN", [tgt], [self.mcu], 0.5, f"S7 LED {node_logic} while firmware runs")
                self.add("WRONG_VALUE", [gpio_net, anode] if gpio_net else [anode], [res], 0.1, "LED/resistor missing or wrong")

        # 7. UART path
        if smoke_uart and banner == "" and usb == "app":
            n = smoke_uart.net
            m = self.probe(n, "r_gnd")
            if m and float(m.value) < 10:
                self.add("PIN_BRIDGE", [n], [self.mcu], 0.75, f"probe r_gnd({n}) {m.value}: TX shorted to GND")
            elif m and (float(m.value) >= OPEN_CIRCUIT_OHM or self.rgnd_ratio(n, float(m.value)) > 1.6):
                self.add("SIGNAL_OPEN", [n], [self.mcu], 0.8, f"probe r_gnd({n}) open: TX pin lifted")
            else:
                self.add("PIN_BRIDGE", [n], [self.mcu], 0.5, "S7 no banner, USB app OK: TX stuck")

        # 8. USB path (firmware runs, USB fails)
        if usb in ("none", "enum_error") and (banner or not smoke_uart):
            usb_nets = [n for n in idx.nets_with_role("usb") if self.tp(n)]
            series = [(ref, a, b, r) for ref, a, b, r in idx.resistors if idx.role(a) == "usb" and idx.role(b) == "usb" and r < 100]
            mcu_side = lambda a, b: a if ic_on_net(idx, a) else b  # noqa: E731
            dp_first = sorted(series, key=lambda s: 0 if re.search(r"(?i)(dp|d\+)", s[1] + s[2]) else 1)
            if usb == "enum_error":
                dp_first = list(reversed(dp_first))
            findings = 0
            for ref, a, b, r in dp_first:
                m = self.probe(a, "r_pair", b)
                if m and float(m.value) > 10 * r:
                    self.add("WRONG_VALUE", [a, b], [ref], 0.85, f"probe r_pair across {ref}={m.value} ohm vs {r:g}")
                    findings += 1
            for ref, a, b, r in dp_first:
                n = mcu_side(a, b)
                m = self.probe(n, "r_gnd")
                if not m:
                    continue
                ratio = self.rgnd_ratio(n, float(m.value))
                if float(m.value) >= OPEN_CIRCUIT_OHM or ratio > 1.6:
                    self.add("SIGNAL_OPEN", [n], [self.mcu], 0.8, f"probe r_gnd({n}) open: USB pin lifted")
                    findings += 1
                elif ratio < 0.05:
                    self.add("PIN_BRIDGE", [n, self.io_rail], [self.mcu], 0.7,
                             f"probe r_gnd({n})={m.value}: tied to a rail")
                    findings += 1
                elif ratio < 0.7:
                    self.add("PIN_BRIDGE", [mcu_side(x, y) for _, x, y, _ in series], [self.mcu], 0.7,
                             f"probe r_gnd({n}) {ratio:.2f}x golden: D+/D- joined")
                    findings += 1
            if not findings:
                self.add("PIN_BRIDGE", [mcu_side(x, y) for _, x, y, _ in series], [self.mcu], 0.4, f"S8 {usb}: USB pins")
                for ref, a, b, r in dp_first[:1]:
                    self.add("SIGNAL_OPEN", [mcu_side(a, b)], [self.mcu], 0.3, f"S8 {usb}")

        # 9. nothing failed on the panel: spend the budget on gpio nets in fixed order (net degree, name)
        if not self.hyps:
            gpios = sorted([n for n in idx.nets_with_role("gpio") if self.tp(n) and nom.get(n) and nom[n].r_gnd],
                           key=lambda n: (-_degree(idx, n), n))
            for n in gpios:
                if self.s.remaining <= 0:
                    break
                m = self.probe(n, "r_gnd")
                if not m:
                    continue
                r = float(m.value)
                if r < 10:
                    self.add("PIN_BRIDGE", [n], [self.mcu], 0.6, f"probe r_gnd({n}) {r:.3g}: shorted to GND")
                elif r >= OPEN_CIRCUIT_OHM or self.rgnd_ratio(n, r) > 1.6:
                    self.add("SIGNAL_OPEN", [n], [self.mcu], 0.6, f"probe r_gnd({n}) open")
                elif self.rgnd_ratio(n, r) < 0.7:
                    self.add("PIN_BRIDGE", [n], [self.mcu], 0.5, f"probe r_gnd({n}) low: joined to another net")
            if not self.hyps:
                self.add("PIN_BRIDGE", gpios[:1], [self.mcu], 0.05, "panel passes: hidden GPIO fault")
        return self.finish()


def triage_rules(obs: Observation, session: ProbeSession) -> List[Hypothesis]:
    return _Rules(obs, session).run()
