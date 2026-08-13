"""A fusion the machine cannot run is staged BY THE COMPILER, not by the author.

Whether a drained tile reaches a vector core depends on the machine's core count
and on the shape, neither of which the trace knows. So the author writes the
fused form once and `compile` turns `fuse` off where it must -- which is what
four hand-written `*_separated` kernels used to do.

The kernels here are FIXTURES, defined inline. Nothing imports the shipped
library: the compiler is what is under test.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel
from kohakutpu.lang.errors import CannotFuse, LangError

from kohakutpu import api
from kohakutpu import lang as L

M, K, N = dims("M, K, N")
LOG2E = 1.4426950408889634


@kernel
def linear_silu(
    x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2, part=8192
):
    """The epilogue on the accumulator -- the shape the author always writes."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * L.recip(L.exp2(acc * -LOG2E) + 1.0)


@kernel
def two_accumulators(
    x=L.In(M, K), w=L.In(N, K), y=L.Out(M, N), *, gm=2, gn=1, nk=2, part=8192
):
    """An epilogue reading TWO tiles: wrong however it is scheduled."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        a = L.tile(gm, gn, nk)
        b = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            a += x[i, k] @ w[j, k]
        for k in loop(x.chunks32(nk)):
            b += x[i, k] @ w[j, k]
        y[i, j] <<= a * b


def plan(kern, m, n, k=128):
    dev = api.script_device("sim")
    x = dev.tensor(np.zeros((m, k), np.float16))
    w = dev.tensor(np.zeros((n, k), np.float16))
    return kern.plan(x, w)


def kinds(compiled) -> list:
    return [s.kind for st in compiled.stages for s in st.instances[0].stmts]


def test_a_grid_the_cores_fit_keeps_the_fused_drain():
    """One tile per core: the accumulator crosses the NoC and never lands."""
    got = plan(linear_silu, 32, 64)
    assert got.knobs.get("fuse", True) is True
    held = [
        s
        for st in got.stages
        for s in st.instances[0].stmts
        if s.kind == "drain" and s.args.get("node")
    ]
    assert held, "no node-addressed drain, so nothing was fused"
    assert not got.temps, f"a fused epilogue needs no temp, got {sorted(got.temps)}"


def test_a_grid_wider_than_the_cores_is_STAGED_not_refused():
    """16 tiles into 4 cores. The author is not asked to pick another kernel."""
    got = plan(linear_silu, 64, 256)
    assert got.knobs.get("fuse", True) is False
    assert got.temps, "staging needs a buffer for the tile to land in"
    node = [
        s
        for st in got.stages
        for s in st.instances[0].stmts
        if s.kind == "drain" and s.args.get("node")
    ]
    assert not node, "still fusing after the fallback"
    assert "apply" in kinds(got), "the epilogue did not survive staging"


def test_the_staged_epilogue_runs_ONCE_over_the_result():
    """Not once per tile.

    `_staged` emits with `top=True` so the pass lands after the tiling rather
    than inside it; nested, it would repeat per tile -- 16x the work, silently.
    """
    got = plan(linear_silu, 64, 256)
    vector = [st for st in got.stages if st.unit == "VC"]
    assert len(vector) == 1, f"{len(vector)} vector stages, expected one pass"


def test_both_shapes_agree_with_numpy():
    """The staged form computes what the fused one would have.

    The point of `_reseat`: only the accumulator's leaf moves, so the author's
    chain is carried across unchanged.
    """
    rng = np.random.default_rng(0)
    dev = api.script_device("sim")
    for m, n in ((32, 64), (64, 256)):
        x = np.asarray(rng.standard_normal((m, 128)) * 0.2, np.float16)
        w = np.asarray(rng.standard_normal((n, 128)) * 0.2, np.float16)
        got = linear_silu(dev.tensor(x), dev.tensor(w)).numpy()
        ref = np.float64(x) @ np.float64(w).T
        want = ref / (1.0 + np.exp(-ref))
        rel = np.abs(got - want) / max(np.abs(want).max(), 1e-9)
        assert np.median(rel) < 5e-2, f"{m}x{n}: p50 {np.median(rel):.3e}"


def test_a_refusal_that_staging_cannot_fix_is_NOT_swallowed():
    """`relax` fires for CannotFuse alone.

    A blanket retry would turn every real defect into a silent slow path, which
    is worse than the refusal it replaced.
    """
    with pytest.raises(LangError) as caught:
        plan(two_accumulators, 32, 64)
    assert not isinstance(caught.value, CannotFuse)
    assert "accumulator" in str(caught.value)
