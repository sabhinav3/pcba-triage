# Lane A — Board & Fixture (Claude)
Owned: `core/triage/{netlist,pcb_pos,pcb_netlist,fixture,faults}.py`, `boards/`, `core/tests/test_{board,faults,fixture}.py`.
Goal: KiCad board -> `board.json` (positions, outline, roles, power tree, test points, golden nominal); behavioral fixture for S1–S8; 30 seeded faults (6×5) picked by net role.
Gate: `make board faults && PYTHONPATH=core .venv/bin/pytest core/tests/test_board.py core/tests/test_faults.py core/tests/test_fixture.py -q`
Done when: every fault changes something probeable, healthy board passes the panel, faults.json is deterministic for SEED.
