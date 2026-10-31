"""Footprint positions, pad positions and Edge.Cuts outline from a .kicad_pcb (KiCad 6+ s-expression format).

No KiCad dependency: a small s-expression reader is enough for (footprint ...), (pad ...) and gr_* items.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

SExpr = Any  # str | list


def parse_sexpr(text: str) -> SExpr:
    tokens: List[Any] = []
    i, n = 0, len(text)
    stack: List[List[Any]] = [[]]
    while i < n:
        c = text[i]
        if c == "(":
            stack.append([])
            i += 1
        elif c == ")":
            done = stack.pop()
            stack[-1].append(done)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            stack[-1].append("".join(buf))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            stack[-1].append(text[i:j])
            i = j
    del tokens
    return stack[0][0] if stack[0] else []


def _children(node: SExpr, head: str) -> List[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def _first(node: SExpr, head: str):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def _xy(node) -> Tuple[float, float, float]:
    vals = [float(v) for v in node[1:4] if isinstance(v, str)]
    while len(vals) < 3:
        vals.append(0.0)
    return vals[0], vals[1], vals[2]


def _footprint_ref(fp: list) -> str | None:
    for p in _children(fp, "property"):
        if len(p) > 2 and p[1] == "Reference":
            return p[2]
    for t in _children(fp, "fp_text"):
        if len(t) > 2 and t[1] == "reference":
            return t[2]
    return None


def _rotate(px: float, py: float, deg: float) -> Tuple[float, float]:
    # KiCad: y grows down, positive angle = counter-clockwise on screen.
    r = math.radians(deg)
    return px * math.cos(r) + py * math.sin(r), -px * math.sin(r) + py * math.cos(r)


def read_pcb(path: str | Path) -> Dict[str, Any]:
    """Returns {"outline": {x0,y0,w,h}, "footprints": {ref: {x,y,rot,side,pads:{pin:(x,y)}}}} in absolute mm."""
    root = parse_sexpr(Path(path).read_text(encoding="utf-8"))
    pts: List[Tuple[float, float]] = []
    for head in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
        for g in _children(root, head):
            layer = _first(g, "layer")
            if not layer or layer[1] != "Edge.Cuts":
                continue
            for key in ("start", "mid", "end", "center"):
                p = _first(g, key)
                if p:
                    pts.append((float(p[1]), float(p[2])))
            poly = _first(g, "pts")
            if poly:
                for xy in _children(poly, "xy"):
                    pts.append((float(xy[1]), float(xy[2])))
    footprints: Dict[str, Any] = {}
    for fp in _children(root, "footprint"):
        ref = _footprint_ref(fp)
        at = _first(fp, "at")
        if not ref or not at:
            continue
        x, y, rot = _xy(at)
        layer = _first(fp, "layer")
        pads: Dict[str, Tuple[float, float]] = {}
        for pad in _children(fp, "pad"):
            pnum = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else ""
            pat = _first(pad, "at")
            if not pnum or not pat or pnum in pads:
                continue
            px, py, _ = _xy(pat)
            dx, dy = _rotate(px, py, rot)
            pads[pnum] = (x + dx, y + dy)
        footprints[ref] = {"x": x, "y": y, "rot": rot, "side": "B" if layer and layer[1].startswith("B.") else "F", "pads": pads}
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, y0 = min(xs), min(ys)
        outline = {"x0": x0, "y0": y0, "w": round(max(xs) - x0, 3), "h": round(max(ys) - y0, 3)}
    else:  # no Edge.Cuts: bounding box of footprints + 2 mm margin
        xs = [f["x"] for f in footprints.values()] or [0.0]
        ys = [f["y"] for f in footprints.values()] or [0.0]
        x0, y0 = min(xs) - 2, min(ys) - 2
        outline = {"x0": x0, "y0": y0, "w": round(max(xs) + 2 - x0, 3), "h": round(max(ys) + 2 - y0, 3)}
    return {"outline": outline, "footprints": footprints}
