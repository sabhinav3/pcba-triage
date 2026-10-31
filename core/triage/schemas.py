"""Frozen contracts (T+0:20). Mirror of shared/types.ts and CONTRACTS.md.

Only the lead changes this file. Lanes import from here; they never redefine shapes.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

PROMPT_VERSION = "v1"  # bump when prompts/triage_agent.md or agent tools change (invalidates LLM cache)

Role = Literal["power_rail", "ground", "clock", "qspi", "usb", "gpio", "reset", "other"]
FaultClass = Literal["RAIL_SHORT", "RAIL_OPEN", "WRONG_VALUE", "CLOCK_FAIL", "PIN_BRIDGE", "SIGNAL_OPEN"]
MeasKind = Literal["v", "r_gnd", "r_pair", "freq", "i_supply", "logic", "uart", "usb_enum"]
Method = Literal["none", "rules", "agent"]
Stage = Literal["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "PROBE"]

FAULT_CLASSES: List[str] = ["RAIL_SHORT", "RAIL_OPEN", "WRONG_VALUE", "CLOCK_FAIL", "PIN_BRIDGE", "SIGNAL_OPEN"]
MAX_NETS_PER_HYPOTHESIS = 3  # scoring guard against "shotgun" hypotheses that list every net
MAX_REFS_PER_HYPOTHESIS = 3
MAX_HYPOTHESES = 5
OPEN_CIRCUIT_OHM = 1e9  # an "OL" reading on the meter

# Simulated bench time per extra probe, seconds (identical for every method).
PROBE_COST_S: Dict[str, float] = {
    "v": 15, "r_gnd": 20, "r_pair": 20, "freq": 45, "logic": 10, "i_supply": 10, "uart": 30, "usb_enum": 30,
}


# ---------- Board (public) ----------
class Pin(BaseModel):
    pin: str
    name: str
    net: str
    x: Optional[float] = None  # pad position, mm, board-outline origin (y grows down)
    y: Optional[float] = None


class Component(BaseModel):
    ref: str
    value: str
    footprint: str
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    dnp: bool = False  # do-not-populate: drawn, but not on the assembled board
    pins: List[Pin] = []


class NetNode(BaseModel):
    ref: str
    pin: str


class Net(BaseModel):
    name: str
    role: Role
    nodes: List[NetNode]


class PowerEdge(BaseModel):
    ref: str
    in_net: str
    out_net: str
    nominal_v: float


class Nominal(BaseModel):
    v: Optional[float] = None          # powered DC level (rails)
    r_gnd: Optional[float] = None      # golden-board unpowered R-to-GND, ohm (None = open / OL)
    r_gnd_min: Optional[float] = None  # rails: below this the rail is suspect
    freq_hz: Optional[float] = None    # clock nets


class TestPoint(BaseModel):
    __test__ = False  # not a pytest class
    tp: str
    net: str
    ref: str
    pin: str
    kinds: List[MeasKind]


class Outline(BaseModel):
    w: float
    h: float


class Board(BaseModel):
    board_id: str
    components: List[Component]
    nets: List[Net]
    power_tree: List[PowerEdge]
    nominal: Dict[str, Nominal]
    testpoints: List[TestPoint]
    outline: Outline


# ---------- Measurements / cases ----------
class Measurement(BaseModel):
    tp: str
    tp2: Optional[str] = None
    kind: MeasKind
    value: Union[float, str]
    unit: str
    stage: Stage


class FaultTarget(BaseModel):
    nets: List[str]
    refs: List[str]
    pins: List[str] = []  # "REF.PIN"


class FaultCase(BaseModel):
    """HIDDEN. Never passed to a triage method, never sent to a browser except in post-run reports."""
    case_id: str
    cls: FaultClass
    target: FaultTarget
    params: Dict[str, Union[float, str, int]] = {}
    seed: int


class Observation(BaseModel):
    board_public: Board
    measurements: List[Measurement]
    probe_budget: int


class ProbeRequest(BaseModel):
    tp: str
    kind: MeasKind
    tp2: Optional[str] = None


class Hypothesis(BaseModel):
    rank: int
    cls: FaultClass
    nets: List[str]
    refs: List[str] = []
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str] = []
    next_probe: Optional[ProbeRequest] = None

    @field_validator("nets")
    @classmethod
    def _cap_nets(cls, v: List[str]) -> List[str]:
        return list(dict.fromkeys(v))[:MAX_NETS_PER_HYPOTHESIS]

    @field_validator("refs")
    @classmethod
    def _cap_refs(cls, v: List[str]) -> List[str]:
        return list(dict.fromkeys(v))[:MAX_REFS_PER_HYPOTHESIS]


class TriageResult(BaseModel):
    case_id: str
    method: Method
    hypotheses: List[Hypothesis] = Field(default_factory=list, max_length=MAX_HYPOTHESES)
    probes: List[Measurement] = []
    probes_used: int = 0
    bench_seconds: float = 0.0
    wall_ms: float = 0.0
    cost_usd: Optional[float] = None
    notes: List[str] = []


# ---------- API request bodies ----------
class TriageRequest(BaseModel):
    method: Method


class BenchRequest(BaseModel):
    methods: List[Method]
    case_ids: Optional[List[str]] = None
