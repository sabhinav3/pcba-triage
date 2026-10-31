"""Fallback netlist export when kicad-cli is not installed: build a kicadxml-style netlist from the .kicad_pcb.

The PCB is the as-built board: every pad carries its net and pin function, and DNP flags live on footprints.
Output follows the kicadxml layout that netlist.build_board reads (components + nets/node[ref,pin,pinfunction]).

CLI: python -m triage.pcb_netlist --pcb <file.kicad_pcb> --out boards/<id>/netlist.xml
"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .pcb_pos import _children, _first, _footprint_ref, parse_sexpr


def _pinfunction(raw: str, pad: str) -> str:
    # KiCad 9+ writes pinfunction as "<name>_<pad>" ("GPIO0_2", "DVDD_23"); strip the pad suffix.
    return raw[: -len(pad) - 1] if pad and raw.endswith("_" + pad) and len(raw) > len(pad) + 1 else raw


def pcb_to_netlist_xml(pcb_path: str | Path) -> ET.ElementTree:
    root = parse_sexpr(Path(pcb_path).read_text(encoding="utf-8"))
    comps: Dict[str, dict] = {}
    nets: Dict[str, Set[Tuple[str, str, str]]] = {}
    for fp in _children(root, "footprint"):
        ref = _footprint_ref(fp)
        if not ref:
            continue
        props = {p[1]: p[2] for p in _children(fp, "property") if len(p) > 2}
        attr = _first(fp, "attr")
        flags = set(attr[1:]) if attr else set()
        comps[ref] = {"value": props.get("Value", ""), "footprint": fp[1], "dnp": "dnp" in flags,
                      "exclude_from_bom": "exclude_from_bom" in flags}
        for pad in _children(fp, "pad"):
            num = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else ""
            n = _first(pad, "net")
            if not num or not n:
                continue
            name = n[-1] if isinstance(n[-1], str) else str(n[-1])
            pf = _first(pad, "pinfunction")
            nets.setdefault(name, set()).add((ref, num, _pinfunction(pf[1], num) if pf else ""))
    export = ET.Element("export", version="E")
    design = ET.SubElement(export, "design")
    ET.SubElement(design, "source").text = str(pcb_path)
    ET.SubElement(design, "tool").text = "pcba-triage pcb_netlist (from .kicad_pcb pads; kicad-cli not available)"
    xc = ET.SubElement(export, "components")

    def natural(r: str) -> Tuple[str, int]:
        m = re.match(r"([A-Za-z_]+)(\d*)", r)
        return (m.group(1), int(m.group(2) or 0)) if m else (r, 0)

    for ref in sorted(comps, key=natural):
        c = comps[ref]
        e = ET.SubElement(xc, "comp", ref=ref)
        ET.SubElement(e, "value").text = c["value"]
        ET.SubElement(e, "footprint").text = c["footprint"]
        if c["dnp"]:
            ET.SubElement(e, "property", name="dnp", value="1")
        if c["exclude_from_bom"]:
            ET.SubElement(e, "property", name="exclude_from_bom", value="1")
    xn = ET.SubElement(export, "nets")
    for code, name in enumerate(sorted(nets), start=1):
        e = ET.SubElement(xn, "net", code=str(code), name=name)
        for ref, pin, pf in sorted(nets[name], key=lambda t: (natural(t[0]), t[1])):
            attrs = {"ref": ref, "pin": pin}
            if pf and pf != pin:
                attrs["pinfunction"] = pf
            ET.SubElement(e, "node", **attrs)
    ET.indent(export)
    return ET.ElementTree(export)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcb", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tree = pcb_to_netlist_xml(args.pcb)
    tree.write(args.out, encoding="UTF-8", xml_declaration=True)
    r = tree.getroot()
    print(f"[netlist] {len(r.find('components'))} components, {len(r.find('nets'))} nets from {args.pcb} -> {args.out}")


if __name__ == "__main__":
    main()
