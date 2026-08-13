"""A `reduce` statement is a chain, so a band may hold it beside an apply.

`Part._reduce` emits its own statement kind, and `_vector` used to build a
`RowReduceKernel` for every one of them -- so a stage split at every `row_max`
and `row_sum` however the hazard guard was set. It now joins the run, and the
band steps ONE ROW at a time because `VRED` folds exactly VL lanes.

The trap this guards is a silent one: a band left at VLMAX for a 64-wide row
folds two rows into one answer and reports success. Every test here grades the
arithmetic rather than reading the image.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, units
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import RowReduceKernel
from kohakutpu.lang import kernel
from kohakutpu.lang.backend import BACKEND
from kohakutpu.lang.errors import LangError
from kohakutpu.model import SimDevice

from kohakutpu import lang as L

FP16 = np.float16
M, N = dims("M, N")


@kernel
def centred(x=L.In(M, N), y=L.Out(M, N), *, part=8192):
    """`y = x - rowsum(x)` in ONE tiling: the reduction and its consumer."""
    t = L.temp(M, N)
    with units(x.parts(part)) as e:
        t[e] <<= L.row_sum(x[e])
        y[e] <<= x[e] - t[e]


@kernel
def centred_apart(x=L.In(M, N), y=L.Out(M, N), *, part=8192):
    """The same arithmetic with the author's own barrier between the halves."""
    t = L.temp(M, N)
    with units(x.parts(part)) as e:
        t[e] <<= L.row_sum(x[e])
    with units(x.parts(part)) as e:
        y[e] <<= x[e] - t[e]


@kernel
def summed(x=L.In(M, N), y=L.Out(M, N)):
    """One reduction and nothing else, which must lower as it always did."""
    y <<= L.row_sum(x)


def operand(shape=(16, 64), seed=3):
    rng = np.random.default_rng(seed)
    return np.asarray(rng.standard_normal(shape) * 0.3, FP16)


def run(fn, array, **knobs):
    """Compile and run `fn` on the model. Returns ``(result, stage count)``."""
    dev = SimDevice(size=64 << 20)
    stages = len(fn.plan(dev.tensor(array), **knobs).stages)
    return np.float64(fn(dev.tensor(array), **knobs).numpy()), stages


def test_a_reduction_and_its_consumer_are_one_program():
    """The whole point: one stage, and the row sum reaches `y` in a register.

    A band that kept VLMAX here would fold rows 0 and 1 together and every row
    of the answer would be wrong by about its own size, so the grading below is
    what pins `vl = cols` rather than any assertion about the image.
    """
    x = operand()
    got, stages = run(centred, x)
    want = np.float64(x) - np.float64(x).sum(axis=1, keepdims=True)
    assert stages == 1
    assert np.abs(got - want).max() / np.abs(want).max() < 3e-3


def test_the_fused_form_is_no_worse_than_the_staged_one():
    """The negative control, and the reason the intermediate is worth removing.

    `t` never lands in fp16 DRAM in the fused form, so it is not merely as
    accurate -- it is measurably better on the same operands.
    """
    x = operand()
    want = np.float64(x) - np.float64(x).sum(axis=1, keepdims=True)
    fused, one = run(centred, x)
    apart, two = run(centred_apart, x)
    assert (one, two) == (1, 2)
    peak = np.abs(want).max()
    assert np.abs(fused - want).max() / peak <= np.abs(apart - want).max() / peak


def test_a_lone_reduction_still_lowers_to_the_reduction_pass():
    """A band of exactly one reduce chain falls back, or every reduction moves.

    `RowReduceKernel` walks whole rows out of L1 and a band of one would emit
    something else, so the shipped kernels' bytes hang on this fallback.
    """
    x = operand()
    dev = SimDevice(size=64 << 20)
    compiled = summed.plan(dev.tensor(x))
    addrs = {n: 0x10000 * (i + 1) for i, n in enumerate(sorted(compiled.layouts))}
    words = BACKEND.encode(compiled, compiled.stages[0], addrs)
    want = RowReduceKernel(OpKind.SUM, 16, 64).flits(addrs["x"], addrs["y"])
    assert next(iter(words.values())) == want


def test_a_row_wider_than_vred_refuses_rather_than_folding_it_twice():
    """`VRED` takes at most VLMAX lanes, so a 256-wide row has no band.

    The refusal names the width. Folding it in two passes would carry a partial
    across them and is not what the tree does.
    """
    with pytest.raises(LangError, match="VRED"):
        run(centred, operand((4, 256)))


# ------------------------------------ B9: one expression, several chains
@kernel
def branching(x=L.In(M, N), y=L.Out(M, N), *, part=8192):
    """`(x - 1) * (x + 1)`: a node whose BOTH arguments are computed."""
    with units(x.parts(part)) as e:
        y[e] <<= (x[e] - 1.0) * (x[e] + 1.0)


@kernel
def repeated(x=L.In(M, N), y=L.Out(M, N), *, part=8192):
    """`d / rowsum(d)` where `d = x - 1`: one subexpression read twice."""
    with units(x.parts(part)) as e:
        d = x[e] - 1.0
        y[e] <<= d / L.row_sum(d)


@kernel
def fold_in_the_lifted_half(x=L.In(M, N), y=L.Out(M, N), *, part=8192):
    """The reduction sits in the argument that gets LIFTED, not the final chain.

    Written second, so the chain kept inline is `x + 1` and the fold moves out
    of the statement's own chain entirely.
    """
    with units(x.parts(part)) as e:
        y[e] <<= (x[e] + 1.0) * (x[e] - L.row_sum(x[e]))


def band_of(fn, array, **knobs):
    """The one band `fn` compiles to, and the statements it holds."""
    from kohakutpu.lang import backend as B

    dev = SimDevice(size=64 << 20)
    compiled = fn.plan(dev.tensor(array), **knobs)
    stmts = list(compiled.stages[0].instances[0].stmts)
    return B._build(compiled, stmts)[0]


def test_a_node_with_two_computed_arguments_lifts_one_into_its_own_chain():
    """B9. One running result, so `(x-1)*(x+1)` is two chains, not a refusal.

    The lifted chain does not store: it was never a buffer, so eliding it is not
    the analysis that eliding a STATEMENT's store would be.
    """
    x = operand()
    band = band_of(branching, x)
    assert len(band.chains) == 2 and band.nout == 1
    assert [c.store for c in band.chains] == [False, True]
    got, stages = run(branching, x)
    want = (np.float64(x) - 1.0) * (np.float64(x) + 1.0)
    assert stages == 1
    assert np.abs(got - want).max() / np.abs(want).max() < 3e-3


def test_a_fold_in_a_lifted_chain_still_sets_the_row_width():
    """MEASURED at 103% error before this was checked, and reported as success.

    `vl` was read off the statement's final chain alone, so a fold that lifted
    left the band at VLMAX and folded rows 0 and 1 together. The band steps a
    row whichever of its chains reduces.
    """
    x = operand()
    assert band_of(fold_in_the_lifted_half, x).vl == 64
    got, stages = run(fold_in_the_lifted_half, x)
    x64 = np.float64(x)
    want = (x64 + 1.0) * (x64 - x64.sum(axis=1, keepdims=True))
    assert stages == 1
    assert np.abs(got - want).max() / np.abs(want).max() < 3e-3


def test_a_subexpression_read_twice_runs_once():
    """`d` is lifted and forwarded to both readers, so it is computed once.

    Two chains rather than three: the reduction stays inside the storing chain,
    since only `d` is read more than once.
    """
    x = operand()
    band = band_of(repeated, x)
    assert len(band.chains) == 2 and len(band.held) == 1
    got, stages = run(repeated, x)
    d = np.float64(x) - 1.0
    want = d / d.sum(axis=1, keepdims=True)
    assert stages == 1
    assert np.abs(got - want).max() / np.abs(want).max() < 3e-3
