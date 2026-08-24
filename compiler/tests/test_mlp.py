"""The gated feed forward, and the fill it does not pay twice.

`geglu` and `swiglu` run both projections on one cluster so `x` stays in L1.
What must hold: the numbers are a matmul's, and the SECOND projection costs no
second fetch of `x`.

The trap this kernel is written around, asserted below: a cluster has ONE
accumulator, so interleaving the sweeps returns a plausible wrong answer.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, loop, units
from kohakutpu.kernels import geglu, swiglu
from kohakutpu.lang import kernel

from kohakutpu import api
from kohakutpu import lang as L

M, K, N = dims("M, K, N")
LOG2E = 1.4426950408889634


def dev():
    return api.script_device("sim")


def operands(m=64, k=128, n=64, seed=0):
    rng = np.random.default_rng(seed)
    return [
        np.asarray(rng.normal(0, 0.3, s), np.float16) for s in ((m, k), (n, k), (n, k))
    ]


def fills_of(kern, args, **knobs) -> int:
    got = kern.plan(*args, **knobs)
    return sum(
        1 for st in got.stages for s in st.instances[0].stmts if s.kind == "fill"
    )


@pytest.mark.parametrize(
    ("kern", "act"),
    [
        (geglu, lambda v: v / (1.0 + np.exp(-1.702 * v))),
        (swiglu, lambda v: v / (1.0 + np.exp(-v))),
    ],
    ids=["geglu", "swiglu"],
)
def test_the_gate_agrees_with_float64(kern, act):
    d = dev()
    x, wg, wu = operands()
    got = np.float64(np.asarray(kern(*[d.tensor(a) for a in (x, wg, wu)]).numpy()))
    g = np.float64(x) @ np.float64(wg).T
    u = np.float64(x) @ np.float64(wu).T
    want = act(g) * u
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-2


@kernel
def _one_projection(
    x=L.In(..., M, K), w=L.In(N, K), h=L.Out(..., M, N), *, gm=8, gn=8, nk=2
):
    """The baseline the shared fill is measured against."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        a = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            a += x[i, k] @ w[j, k]
        h[i, j] <<= a


def test_the_shared_fill_of_x_is_GIVEN_UP_and_why():
    """It bought a WRONG ANSWER past two K-chunks, so `geglu` pays for `x` twice.

    Both halves in one grid is two accumulator tiles alive at once, and a
    cluster has one. It survives a one- or two-chunk sweep -- which is where the
    kernel was measured clean -- and returns partial sums from three on. SDXL's
    feed-forward is ten to twenty chunks, so it was wrong at every real width.
    """
    d = dev()
    x, wg, wu = operands()
    args = [d.tensor(a) for a in (x, wg, wu)]
    one = fills_of(_one_projection, [args[0], args[1]])
    both = fills_of(geglu, args)
    assert one == 4, one
    assert both == 2 * one, both


@pytest.mark.parametrize("k", [128, 192, 256, 640])
def test_geglu_is_RIGHT_at_every_K_not_just_a_two_chunk_one(k):
    """The regression this exists for: it was clean at K=128 and nowhere else.

    Graded on the SHARE past 1%, not the max: the fused form's max at K=256 was
    8.8e-1 with 8.8% of elements past 10%, against 1.6e-2 and nothing past 10%
    here, and its p50 was 34x worse even where the max still looked passable.
    """
    m, n = 64, 256
    rng = np.random.default_rng(4)
    x = np.asarray(rng.standard_normal((m, k)), np.float16)
    wg = np.asarray(rng.standard_normal((n, k)) * 0.06, np.float16)
    wu = np.asarray(rng.standard_normal((n, k)) * 0.06, np.float16)
    d = dev()
    got = np.float64(geglu(*[d.tensor(a) for a in (x, wg, wu)]).numpy())
    g = np.float64(x) @ np.float64(wg).T
    u = np.float64(x) @ np.float64(wu).T
    want = g / (1.0 + np.exp(-1.702 * g)) * u
    err = np.abs(got - want) / np.abs(want).max()
    assert float((err > 0.10).mean()) == 0.0
    assert float((err > 0.01).mean()) < 0.02
    assert float(np.percentile(err, 50)) < 1e-3


@kernel
def _interleaved(
    x=L.In(..., M, K),
    wa=L.In(N, K),
    wb=L.In(N, K),
    ha=L.Out(..., M, N),
    hb=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """Both sweeps in ONE loop -- what `mlp.py` is written to avoid."""
    with units(x.tiles(gm), wa.tiles(gn)) as (i, j):
        a = L.tile(gm, gn, nk)
        b = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            a += x[i, k] @ wa[j, k]
            b += x[i, k] @ wb[j, k]
        ha[i, j] <<= a
        hb[i, j] <<= b


def test_interleaving_the_sweeps_returns_a_PLAUSIBLE_WRONG_ANSWER():
    """One accumulator, so the second GEMM's reset destroys the first tile.

    It compiles and it runs. Nothing faults, no NaN, no saturation -- the
    numbers are simply wrong, which is why `geglu` drains between the sweeps.
    """
    d = dev()
    x, wa, wb = operands()
    ha, _ = _interleaved(*[d.tensor(a) for a in (x, wa, wb)])
    got = np.float64(np.asarray(ha.numpy()))
    want = np.float64(x) @ np.float64(wa).T
    assert not np.isnan(got).any() and not np.isinf(got).any()
    assert np.abs(got - want).max() / np.abs(want).max() > 0.5
