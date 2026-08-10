"""Level 3 -> machine code, checked against the planner the RTL actually runs.

`tests/ktpu/unit/test_encode.py` checks the bit LAYOUT field by field. This
checks the whole STREAM: for one 64x64x64 GEMM on one cluster, every flit ktpu
emits is byte-identical to the one `driver.bench.build` emits -- and that stream
is what `xsim.py mag_driver` executes. Identical bytes mean the RTL cannot tell
the two apart.

The reconciliation the two planners need -- word units, `preq`, `emit` -- lives
in `ktpu.hw.fromdsl`, which is also what `scripts/py/run_dsl.py` stages into a
real simulation. Byte-identity here and a scored answer there are then two
claims about the same code rather than about two reimplementations of it.
"""

import ktpu.dsl as D
from ktpu.hw import fromdsl
from ktpu.hw.fromdsl import ONE_CU
from ktpu.ir import FP16
from ktpu.passes import lower


def _reference(m, k, n):
    """The planner whose output xsim runs: `ktpu.hw`, the RTL-facing half."""
    from ktpu.hw import bench

    return bench.build(m, k, n, ncl=1)


def _ktpu_flits(prob, m, k, n):
    return fromdsl.flits(prob, m, k, n)


def test_the_flit_stream_is_byte_identical_to_the_planner_xsim_runs():
    """The whole of `level 3 -> machine code`, end to end. If this passes, the
    bytes xsim executes for this GEMM are the bytes ktpu produced."""
    prob = _reference(64, 64, 64)
    reference = [f for r in prob.kern.rounds for p in r.passes for f in p.flits]
    mine = _ktpu_flits(prob, 64, 64, 64)

    assert len(mine) == len(reference) == 4
    for i, (want, got) in enumerate(zip(reference, mine, strict=True)):
        assert got == want, f"flit {i}\n  planner {want:#x}\n  ktpu    {got:#x}"


def test_both_pick_the_same_tile():
    """The comparison is only meaningful if the two planners agree on the tile;
    if they ever diverge this fails loudly rather than comparing two different
    decompositions and calling the difference an encoding bug."""
    prob = _reference(64, 64, 64)
    graph = D.trace(
        lambda a, b: a @ b, D.TensorSpec((64, 64), FP16), D.TensorSpec((64, 64), FP16)
    )
    band = lower(graph, ONE_CU).bands[0]
    assert (band.tile.m, band.tile.k, band.tile.n) == (64, 64, 64)
    assert str(prob.kern.tile) == "64x64x64"


def test_the_stream_is_one_fill_per_operand_then_sweep_then_drain():
    prob = _reference(64, 64, 64)
    mine = _ktpu_flits(prob, 64, 64, 64)
    ops = [(w >> 252) & 0xF for w in mine]
    assert ops == [1, 1, 2, 3]
    assert [w >> 259 & 1 for w in mine] == [0, 0, 0, 1]
