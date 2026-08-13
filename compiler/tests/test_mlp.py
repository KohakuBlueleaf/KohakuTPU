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


def test_the_second_projection_costs_no_second_fill_of_x():
    """THE claim. Two projections apart fetch `x` twice; together, once.

    Six fills rather than eight at this shape: `x` for each of two K chunks,
    plus `wg` and `wu` for each. Apart it would be four and four.
    """
    d = dev()
    x, wg, wu = operands()
    args = [d.tensor(a) for a in (x, wg, wu)]
    one = fills_of(_one_projection, [args[0], args[1]])
    both = fills_of(geglu, args)
    assert one == 4, one
    assert both == 6, both
    assert both < 2 * one, "the shared operand was fetched twice after all"


def test_a_shared_operand_is_filled_once_per_BANK_not_once_per_kernel():
    """The dedup is per L1 slot, so each K chunk still fills `x` for its bank.

    Collapsing further would hand the second sweep the wrong K block.
    """
    d = dev()
    x, wg, wu = operands()
    got = geglu.plan(*[d.tensor(a) for a in (x, wg, wu)])
    xs = [
        s
        for st in got.stages
        for s in st.instances[0].stmts
        if s.kind == "fill" and s.args["operand"] == "x"
    ]
    assert len(xs) == 2, [s.args["chunk"] for s in xs]
    assert {s.args["chunk"] for s in xs} == {0, 1}


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
