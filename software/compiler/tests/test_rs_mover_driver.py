"""The retained driver's six mover streams equal the Rust ISA encoder."""

import json

import pytest

from kohakuaccel.driver.device import mover as DM
from kohakutpu.compiler import _rs

MACHINE = "multimesh_v9t6"
WALK = [(4, 1 << 14), (16, 32)]
SRC = {
    "base": 0x10_0000,
    "dims": [(3, 1 << 12, 1, 1), (8, 32, 2, -1)],
    "windows": [(-1, 2), (0, 7)],
}
DST = {"base": 0x20_0000, "dims": [(24, 32)]}
DRIVER_SRC = DM.Walker(
    0x10_0000,
    dims=[(3, 1 << 12), (8, 32)],
    axes=[(1, 1), (2, -1)],
    window={1: (-1, 2), 2: (0, 7)},
)
DRIVER_DST = DM.Walker(0x20_0000, dims=[(24, 32)])
CASES = [
    (
        {
            "fn": "move",
            "args": [DM.COPY, [0x10_0000, WALK], [0x20_0000, WALK]],
            "kw": {"ewidth": DM.W32},
        },
        DM.copy(
            DM.Walker(0x10_0000, dims=WALK),
            DM.Walker(0x20_0000, dims=WALK),
            ewidth=DM.W32,
        ),
    ),
    (
        {
            "fn": "move",
            "args": [DM.COPY, SRC, DST],
            "kw": {"ewidth": DM.W16, "imm": 0x3C00, "flags": 3 << 5},
        },
        DM.copy(DRIVER_SRC, DRIVER_DST, ewidth=DM.W16, imm=0x3C00, flags=3 << 5),
    ),
    (
        {"fn": "fill", "args": [DST, 7, DM.W8]},
        DM.fill(DRIVER_DST, 7, ewidth=DM.W8, flags=DM.FLAG_WCOAL),
    ),
    (
        {"fn": "generate", "args": [DST, 0x1234]},
        DM.program(DM.GENERATE, None, DRIVER_DST, ewidth=DM.W32, seed=0x1234),
    ),
    (
        {
            "fn": "gather",
            "args": [0x30_0000, [0x40_0000, 9], DST, [256, 3]],
            "kw": {"fp": True},
        },
        DM.program(
            DM.GATHER,
            DM.Walker(0x30_0000, dims=[(1, 32)]),
            DRIVER_DST,
            ewidth=DM.W32,
            index=(0x40_0000, 9),
            gather=(256, 3),
            fp=True,
        ),
    ),
    (
        {"fn": "scatter", "args": [DST, 0x30_0000, [0x40_0000, 9], [256, 3]]},
        DM.program(
            DM.SCATTER,
            DRIVER_DST,
            DM.Walker(0x30_0000, dims=[(1, 32)]),
            ewidth=DM.W32,
            index=(0x40_0000, 9),
            gather=(256, 3),
        ),
    ),
]


def encoded(inp, mutant=None):
    got = json.loads(
        _rs.spec_run("isa.mover.move", json.dumps(inp), MACHINE, mutant=mutant)
    )
    return [(int(r, 16), int(v, 16)) for r, v in got["writes"]]


@pytest.mark.parametrize(
    "inp,want",
    CASES,
    ids=[
        "copy32",
        "copy-axes-windows-pad-burst",
        "fill8",
        "generate",
        "gather-fp",
        "scatter",
    ],
)
def test_driver_matches_rust(inp, want):
    assert encoded(inp) == want


def test_driver_comparison_detects_a_changed_register():
    inp, want = CASES[0]
    assert encoded(inp, mutant="isa.mover/hdr") != want
