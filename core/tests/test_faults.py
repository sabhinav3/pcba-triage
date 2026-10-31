from collections import Counter

from triage.faults import generate
from triage.schemas import FAULT_CLASSES


def test_thirty_cases_five_per_class(cases):
    assert len(cases) == 30
    assert Counter(c.cls for c in cases) == {k: 5 for k in FAULT_CLASSES}
    assert len({c.case_id for c in cases}) == 30


def test_deterministic_and_matches_committed(board, cfg, cases):
    a = generate(board, cfg, 2026)
    b = generate(board, cfg, 2026)
    assert [x.model_dump() for x in a] == [x.model_dump() for x in b]
    assert [x.model_dump() for x in a] == [x.model_dump() for x in cases]


def test_case_ids_are_opaque(cases):
    for c in cases:
        assert c.case_id.startswith("c") and c.case_id[1:].isdigit()
    first_class_order = [c.cls for c in sorted(cases, key=lambda c: c.case_id)][:6]
    assert first_class_order != FAULT_CLASSES  # shuffled, ids do not encode class


def test_targets_come_from_netlist(board, cases):
    nets = {n.name for n in board.nets}
    refs = {c.ref for c in board.components}
    for c in cases:
        assert set(c.target.nets) <= nets and set(c.target.refs) <= refs and c.target.nets
