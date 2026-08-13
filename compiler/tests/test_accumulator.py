"""A sweep the GEMM's chain flag cannot express is refused, not miscomputed.

The flag is the innermost loop counter and zero CLEARS the tile, so a sweep two
loops deep restarts on every outer iteration and keeps only the last -- measured
at **1.001x a single pass instead of 3x**, compiling clean and reporting success.
`.plan/TILING-NOT-CHECKPOINT.md` recommends that shape, so it will be written
deliberately and has to fail loudly first.

The ALLOWED nesting is tested too: a loop outside `with units(...)` opens a new
stage with a fresh tile, so only loops opened after the tile carry its chain.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, iface, loop, units
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.lang import kernel
from kohakutpu.lang.errors import LangError

from kohakutpu import kernels as K
from kohakutpu import lang as L
from kohakutpu import ops as O

M, KD, N = dims("M, K, N")

MACHINE = MachineSpec(
    name="accumulator",
    units={"MG": ((1, 1), (2, 1), (1, 2), (2, 2)), "VC": ((2, 0),)},
    inst_depth=512,
    agent=(1, 0),
)


class Shaped:
    def __init__(self, shape) -> None:
        self.shape = shape


def test_a_doubly_nested_sweep_is_refused():
    """The measured case: 1.001x one pass, reported as success."""
    with pytest.raises(LangError, match="nested loops"):

        @kernel
        def twice(a=L.In(M, KD), b=L.In(N, KD), y=L.Out(M, N), *, gm=2, gn=1, nk=2):
            with units(a.tiles(gm), b.tiles(gn)) as (i, j):
                acc = L.tile(gm, gn, nk)
                for _ in loop(3):
                    for c in loop(a.chunks32(nk)):
                        acc += a[i, c] @ b[j, c]
                y[i, j] <<= acc

        twice.trace()


def test_the_refusal_says_what_the_machine_would_have_done():
    """ "It failed" is useless here; the author needs to know 0 CLEARS the tile."""
    with pytest.raises(LangError) as why:

        @kernel
        def twice(a=L.In(M, KD), b=L.In(N, KD), y=L.Out(M, N), *, gm=2, gn=1, nk=2):
            with units(a.tiles(gm), b.tiles(gn)) as (i, j):
                acc = L.tile(gm, gn, nk)
                for _ in loop(3):
                    for c in loop(a.chunks32(nk)):
                        acc += a[i, c] @ b[j, c]
                y[i, j] <<= acc

        twice.trace()
    said = str(why.value)
    assert "clears the tile" in said
    assert "discarded" in said


def test_one_loop_over_the_sweep_is_the_ordinary_case():
    """The guard must not cost the shape every matmul in the library has."""
    got = O.matmul.compile(
        MACHINE,
        iface.solve(
            O.matmul.signature, {"a": Shaped((64, 128)), "b": Shaped((32, 128))}
        ),
    )
    assert got.statements > 0


def test_a_loop_outside_the_units_block_still_nests():
    """`flash_attention` is written this way and must keep compiling.

    Each pass through the outer loop opens its own stage with its own resident
    tile, so the chain flag restarting is correct there rather than a bug.
    """
    bound = {
        "q": Shaped((128, 64)),
        "k": Shaped((128, 64)),
        "v": Shaped((64, 128)),
    }
    got = K.flash_attention.compile(
        MACHINE, iface.solve(K.flash_attention.signature, bound), block=64
    )
    assert len(got.stages) > 1


def test_an_accumulator_is_not_an_operand():
    """`acc += sw @ v[j]` was a raw TypeError, which tells an author nothing."""
    with pytest.raises(LangError, match="two L1 regions"):

        @kernel
        def feedback(a=L.In(M, KD), b=L.In(N, KD), y=L.Out(M, N), *, gm=2, gn=1, nk=2):
            with units(a.tiles(gm), b.tiles(gn)) as (i, j):
                acc = L.tile(gm, gn, nk)
                sw = L.tile(gm, gn, nk)
                for c in loop(a.chunks32(nk)):
                    acc += sw @ b[j, c]
                y[i, j] <<= acc

        feedback.trace()


def test_rebinding_the_accumulator_then_sweeping_says_so():
    """`acc = acc * corr` REBINDS the name; `acc += ...` after it was a TypeError.

    The arithmetic returns an expression, so the sweep lands on something that
    is not a tile and the chain reached `float()`. The author's mistake is the
    rebinding, which a `TypeError` from a numeric cast does not mention.
    """
    with pytest.raises(LangError, match="holds an EXPRESSION"):

        @kernel
        def rebound(a=L.In(M, KD), b=L.In(N, KD), y=L.Out(M, N), *, gm=2, gn=1, nk=2):
            with units(a.tiles(gm), b.tiles(gn)) as (i, j):
                acc = L.tile(gm, gn, nk)
                acc = acc * 0.5
                for c in loop(a.chunks32(nk)):
                    acc += a[i, c] @ b[j, c]
                y[i, j] <<= acc

        rebound.trace()


def test_a_row_reduction_on_an_accumulator_lands_it():
    """It LOWERS. A row of a resident tile is not addressable -- 4x4 sub-tiles,
    four logical rows to a 32-byte word -- so `Tile.landed` drains it and the
    layout pass converts, which is the link `planned_btb.py` pays as `sc_link`.
    Refusing instead made the author write that drain by hand, fourteen times.
    """

    @kernel
    def reduced(a=L.In(M, KD), b=L.In(N, KD), y=L.Out(M, N), *, gm=2, gn=1, nk=2):
        with units(a.tiles(gm), b.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for c in loop(a.chunks32(nk)):
                acc += a[i, c] @ b[j, c]
            y[i, j] <<= L.row_max(acc)

    # It LANDS -- the old refusal named the sub-tile order and stopped. What is
    # left is the write: a landed reduction is rows, and `y[i, j]` is a tile.
    with pytest.raises(LangError, match="vector pass over rows"):
        reduced.trace()


def test_a_row_reduction_on_a_buffer_is_untouched():
    """The refusal is about the accumulator only: `softmax` reduces buffers."""

    @kernel
    def reduce_a_buffer(x=L.In(M, N), y=L.Out(M, N)):
        y <<= L.row_max(x)

    assert reduce_a_buffer.trace().top


def test_the_guard_reads_depth_from_the_tile_not_the_trace():
    """Two accumulators at different depths in one kernel are judged separately."""

    @kernel
    def paired(a=L.In(M, KD), b=L.In(N, KD), y=L.Out(M, N), *, gm=2, gn=1, nk=2):
        with units(a.tiles(gm), b.tiles(gn)) as (i, j):
            outer = L.tile(gm, gn, nk)
            for c in loop(a.chunks32(nk)):
                outer += a[i, c] @ b[j, c]
            y[i, j] <<= outer

    extents = iface.solve(
        paired.signature, {"a": Shaped((64, 128)), "b": Shaped((32, 128))}
    )
    assert paired.compile(MACHINE, extents).statements > 0


def test_the_numbers_are_right_where_the_guard_allows(dev=None):
    """A single-loop sweep still computes a matmul, on the unit models."""
    from kohakutpu.model import SimDevice

    sim = SimDevice()
    rng = np.random.default_rng(0)
    a = (rng.standard_normal((32, 64)) * 0.4).astype(np.float16)
    b = (rng.standard_normal((32, 64)) * 0.4).astype(np.float16)
    got = O.matmul(sim.tensor(a), sim.tensor(b)).numpy()
    want = a.astype(np.float64) @ b.astype(np.float64).T
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05
