import pytest

from triage.fixture import BudgetExhausted, Fixture, ProbeSession


def panel_map(fx):
    return {(m.tp, m.kind): m.value for m in fx.panel()}


def test_healthy_board_passes(board, cfg):
    p = panel_map(Fixture(board, cfg, None))
    assert p[("USB_HOST", "usb_enum")] == "app"
    assert p[("TP_GPIO0", "uart")] == "BOOT OK fw=1.0"
    assert abs(p[("TP_Xin", "freq")] - 12e6) < 12e6 * 30e-6
    assert abs(p[("TP_3_3V", "v")] - 3.3) < 0.15
    assert 15 < p[("SUPPLY", "i_supply")] < 40


def test_every_fault_changes_something_probeable(board, cfg, cases):
    healthy = Fixture(board, cfg, None)
    for c in cases:
        fx = Fixture(board, cfg, c)
        net = c.target.nets[0]
        tp = next(t.tp for t in board.testpoints if t.net == net and t.tp.startswith("TP_"))
        diffs = [m for m, h in zip(fx.panel(), healthy.panel()) if m.value != h.value and isinstance(m.value, str)]
        r_f, r_h = fx.measure(tp, "r_gnd").value, healthy.measure(tp, "r_gnd").value
        assert diffs or abs(r_f / r_h - 1) > 0.2 or c.cls == "WRONG_VALUE", c.case_id


def test_signatures(board, cfg, cases):
    for c in cases:
        p = panel_map(Fixture(board, cfg, c))
        if c.cls == "RAIL_SHORT":
            assert p[("SUPPLY", "i_supply")] > 140
        if c.cls == "CLOCK_FAIL":
            assert p[("TP_Xin", "freq")] == 0 and p[("USB_HOST", "usb_enum")] == "none"
        if c.cls == "RAIL_OPEN":
            assert p[("SUPPLY", "i_supply")] < 5


def test_noise_is_seeded(board, cfg, cases):
    a, b = Fixture(board, cfg, cases[0]), Fixture(board, cfg, cases[0])
    assert [m.model_dump() for m in a.panel()] == [m.model_dump() for m in b.panel()]


def test_budget_enforced(board, cfg, cases):
    s = ProbeSession(Fixture(board, cfg, cases[0]), budget=2)
    s.probe("TP_3_3V", "v")
    with pytest.raises(ValueError):
        s.probe("TP_3_3V", "freq")  # invalid kind does not consume budget
    s.probe("TP_1V1", "r_gnd")
    with pytest.raises(BudgetExhausted):
        s.probe("TP_VCC", "v")
    assert len(s.probes) == 2 and s.bench_seconds == 35
