"""The runtime on the device double: real kernels on the L2 simulator, no hardware."""

import os
import pathlib
import sys

import pytest

# A composed read compiles to L1 to learn it lowers: a model's reads stay materialized.
os.environ.setdefault("KTPU_COMPOSED", "off")

from kohakutpu.application import ktpugrad
from kohakutpu.application.runtime import device as card

#: The stages' machine, so a harvest fits their corpus.
BOARD = "multimesh_v9t8"
APPLICATION = pathlib.Path(__file__).resolve().parents[2] / "kohakutpu" / "application"
sys.path.insert(0, str(APPLICATION))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


@pytest.fixture
def dev():
    ktpugrad.install()
    d = card.Device(kind="double", board=BOARD, nodes=1)
    with card.attached(d):
        yield d
    d.close()
