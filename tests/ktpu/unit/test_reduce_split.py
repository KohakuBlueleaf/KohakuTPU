"""A reduction wider than one vector pass, cut into two that fit.

GroupNorm(32) at C=320, HW=64 reduces 640 elements and VLMAX is 128. Before
`passes.reduce.split_wide` that was a ScheduleError, which made every SDXL
resblock unschedulable; the arithmetic never needed the TREE to carry a partial.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.dsl.library import layernorm, rmsnorm
from ktpu.interp import run_one
from ktpu.ir import FP16, OpKind
from ktpu.ir.graph import REDUCTION
from ktpu.passes import lower
from ktpu.passes.reduce import chunk_width, split_wide
from ktpu.target import VU13P_8CU

T = VU13P_8CU
RNG = np.random.default_rng(19)


def spec(*s):
    return D.TensorSpec(s, FP16)


def widths(graph):
    """Elements each reduction in `graph` folds."""
    out = []
    for op in graph.ops:
        if op.kind in REDUCTION:
            n = 1
            for a in op.attrs["axes"]:
                n *= op.inputs[0].shape[a]
            out.append(n)
    return out


@pytest.mark.parametrize(
    "span,want", [(640, 128), (320, 80), (1280, 128), (128, 128), (129, 43)]
)
def test_the_chunk_is_the_largest_divisor_that_fits(span, want):
    assert chunk_width(span, 128) == want


def test_a_prime_wider_than_one_pass_has_no_split():
    assert chunk_width(257, 128) == 0


@pytest.mark.parametrize("width", [256, 320, 640, 1280])
def test_a_wide_row_becomes_two_reductions_that_fit(width):
    g = D.trace(lambda x: x.sum(axis=-1, keepdim=True) * 1.0, spec(8, width))
    assert widths(g) == [width]
    assert split_wide(g, 128) == 1
    assert len(widths(g)) == 2
    assert all(w <= 128 for w in widths(g))


def test_the_split_does_not_change_the_answer():
    x = RNG.standard_normal((8, 640))
    g = D.trace(lambda a: a.sum(axis=-1, keepdim=True) * 1.0, spec(8, 640))
    before = run_one(g, x)
    split_wide(g, 128)
    assert np.abs(run_one(g, x) - before).max() < 1e-9


def test_sumsq_folds_its_chunk_totals_with_a_plain_sum():
    """Squaring the partials again is the obvious wrong second stage."""
    x = RNG.standard_normal((8, 640))
    g = D.trace(lambda a: a.sumsq(axis=-1, keepdim=True) * 1.0, spec(8, 640))
    split_wide(g, 128)
    kinds = [op.kind for op in g.ops if op.kind in REDUCTION]
    assert kinds == [OpKind.SUMSQ, OpKind.SUM]
    assert np.abs(run_one(g, x) - (x * x).sum(-1, keepdims=True)).max() < 1e-9


def test_rmax_splits_and_stays_exact():
    x = RNG.standard_normal((4, 512))
    g = D.trace(lambda a: a.max(axis=-1, keepdim=True) * 1.0, spec(4, 512))
    split_wide(g, 128)
    assert np.array_equal(run_one(g, x), x.max(-1, keepdims=True))


def test_the_rewrite_is_idempotent():
    g = D.trace(lambda a: a.sum(axis=-1, keepdim=True) * 1.0, spec(8, 640))
    assert split_wide(g, 128) == 1
    assert split_wide(g, 128) == 0


def test_a_reduction_over_an_interior_axis_is_left_alone():
    """The split is a reshape, and reshaping around an interior axis is not a
    view -- `lower` still rejects it, naming the real limit."""
    g = D.trace(lambda a: a.sum(axis=1, keepdim=True) * 1.0, spec(4, 640, 2))
    assert split_wide(g, 128) == 0


def test_a_65k_row_needs_three_stages():
    """One pass of the rewrite leaves 512 chunk totals, still four passes."""
    g = D.trace(lambda a: a.sum(axis=-1, keepdim=True) * 1.0, spec(2, 65536))
    split_wide(g, 128)
    assert all(w <= 128 for w in widths(g))


@pytest.mark.parametrize("width", [320, 640, 1280])
def test_layernorm_and_rmsnorm_lower_at_sdxl_widths(width):
    for fn, extra in ((rmsnorm, 1), (layernorm, 2)):
        specs = [spec(64, width)] + [spec(width)] * extra
        sched = lower(D.trace(fn, *specs), T)
        assert sched.bands, f"{fn.__name__} at {width} produced no bands"


def test_lowering_twice_agrees():
    """`lower` rewrites the graph in place, so a caller that lowers for two
    targets must not get a different schedule the second time."""
    g = D.trace(rmsnorm, spec(64, 640), spec(640))
    first = [len(b.ops) for b in lower(g, T).bands]
    assert [len(b.ops) for b in lower(g, T).bands] == first
