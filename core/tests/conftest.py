import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triage.faults import load_board, load_faults  # noqa: E402
from triage.netlist import load_cfg  # noqa: E402

BOARD_ID = "rp2040_easyduino"


@pytest.fixture(scope="session")
def board():
    return load_board(BOARD_ID)


@pytest.fixture(scope="session")
def cfg():
    return load_cfg(BOARD_ID)


@pytest.fixture(scope="session")
def cases():
    return load_faults(BOARD_ID)
