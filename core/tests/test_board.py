from triage.netlist import BoardIndex, parse_si


def test_parse_si():
    assert parse_si("5.1k") == 5100
    assert parse_si("27") == 27
    assert abs(parse_si("100nF") - 1e-7) < 1e-15
    assert abs(parse_si("27p") - 27e-12) < 1e-20
    assert parse_si("4k7") == 4700
    assert parse_si("12MHz") == 12e6


def test_board_shape(board):
    idx = BoardIndex(board)
    assert len(board.components) == 65 and len(board.nets) == 61
    assert board.outline.w > 10 and board.outline.h > 10
    edges = {(e.ref, e.in_net, e.out_net) for e in board.power_tree}
    assert edges == {("D1", "VBUS", "VCC"), ("U1", "VCC", "+3.3V"), ("U2", "+3.3V", "+1V1")}
    assert idx.rail_order() == ["VBUS", "VCC", "+3.3V", "+1V1"]
    assert idx.ground == "GND"
    assert idx.role("/QSPI_SCLK") == "qspi" and idx.role("/Xin") == "clock" and idx.role("/RUN") == "reset"
    assert idx.role("unconnected-(U3-EP-Pad9)") == "other"
    assert idx.comp["R8"].dnp and not idx.comp["R9"].dnp  # DNP parts come from the PCB attributes
    u2 = idx.comp["U2"]
    assert all(0 <= c.x <= board.outline.w and 0 <= c.y <= board.outline.h for c in board.components if c.pins)
    assert u2.pins[0].x is not None


def test_testpoints(board):
    idx = BoardIndex(board)
    for rail in ("VBUS", "VCC", "+3.3V", "+1V1"):
        assert "r_gnd" in idx.tp[idx.tp_of_net[rail]].kinds
    assert "uart" in idx.tp[idx.tp_of_net["/GPIO0"]].kinds
    assert idx.tp[idx.tp_of_net["/Xin"]].ref == "TP3"  # real test point pads are preferred
    assert "SUPPLY" in idx.tp and "USB_HOST" in idx.tp
    assert board.nominal["+3.3V"].v == 3.3 and board.nominal["/QSPI_SD0"].r_gnd == 300000.0


def test_adjacent_pins_same_side_only(board):
    pairs = BoardIndex(board).adjacent_pin_pairs("U2")
    assert ("1", "2") in pairs and ("13", "14") in pairs
    assert ("14", "15") not in pairs and ("56", "57") not in pairs  # corner / exposed pad
