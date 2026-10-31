"""What a launch reads from the Rust plans: mask tables as f16 bits, a walk
pair no form takes, and a rows kernel's column blocks and output extent."""

from types import SimpleNamespace

import numpy as np
import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.machine import machine_of

DEV = SimpleNamespace(machine=machine_of("vlt_v9t6s_1n"))
F16_ONE = 0x3C00
#: A [32, 64] read of a [64, 32] source.
TRANSPOSE = (2048, ((32, 1), (64, 32)), 0, ((32, 64), (64, 1)), 0, 2048, False)
#: A walk pair with no axis contiguous on either side.
NO_FORM = (64, ((8, 7), (4, 3)), 0, ((8, 5), (4, 2)), 0, 64, False)


@pytest.mark.parametrize(
    "gen, call",
    [
        ("relayout.plan", TRANSPOSE),
        ("lanes.plan", NO_FORM),
    ],
)
def test_mask_tables_are_f16_bits(gen, call):
    """A selected lane is 1.0 in f16: the plan's values read as integers
    select nothing."""
    masks = G.planned(DEV, G.relayout_plan(gen, *call))["masks"]
    assert set(np.unique(masks).tolist()) == {0, F16_ONE}


def test_transpose_masks_are_f16_bits():
    assert set(np.unique(G.transpose_masks(DEV)).tolist()) == {0, F16_ONE}


def test_a_walk_no_form_takes_is_not_planned():
    n, walk, at, dwalk, dat, m, _ = NO_FORM
    no_form = FM.Step(0, n, walk, at, dwalk, dat, m)
    planned = FM.Step(0, *TRANSPOSE[:-1])
    assert FM.plans_of(DEV, (planned, no_form)) is None
    assert FM.plans_of(DEV, (planned, planned))[-1]["index"].dtype == np.float32


@pytest.mark.parametrize("W, blocks", [(128, 1), (1024, 8)])
def test_rows_shape_is_the_kernels_blocks_and_extent(W, blocks):
    e = X.Reduce("sum", X.Load(0))
    got = G.rows_shape(DEV, e, [Operand("full", pitch=W)], 32, W, Operand("row"))
    assert got["nblk"] == blocks
    assert got["out"] == ([blocks, 32] if blocks > 1 else [32])
