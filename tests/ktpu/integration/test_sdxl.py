"""The SDXL primitives, executed on the simulated machine and scored.

Everything here runs the real instruction stream -- the same addresses, L1
spans, loop counts and operand bindings -- against the float64 reference at
level 1. `test_views.py` says the descriptors name the right elements; this
says the machine reading them produces the right numbers.

`scratch/sdxl-fwd/conv_card.py` runs the same conv2d descriptors on the FPGA:
p50 2.5e-3 against a direct convolution in fp64, at 8x8x320 -> 8x8x320.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.dsl import nn
from ktpu.dsl.library import (
    geglu,
    groupnorm,
    layernorm,
    nearest2x_matrix,
    upsample_nearest2x,
)
from ktpu.interp import run_one
from ktpu.interp.mesh import Mesh
from ktpu.ir import FP16
from ktpu.ir.sched import ScheduleError
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

T = VU13P_8CU
RNG = np.random.default_rng(23)

#: FP16 in memory and MXFP7 through the matmul, so a whole-tensor relative
#: error under half a percent is the format's floor, not the compiler's.
TOL = 5e-3


def spec(*s):
    return D.TensorSpec(s, FP16)


def execute(fn, specs, arrays, m, k, n):
    """Trace, lower, codegen, run. Returns `(result, reference, program)`."""
    graph = D.trace(fn, *specs)
    sched = lower(graph, T)
    want = run_one(graph, *arrays)
    prog = codegen(sched, m, k, n, T)
    mesh = Mesh(prog, T)
    for v, arr in zip(graph.inputs, arrays, strict=True):
        mesh.bind(graph.producer(v).attrs["name"], arr)
    mesh.run()
    return mesh.result().reshape(np.shape(want)), want, prog


def rel(got, want):
    return float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-12))


# ---- conv2d ----------------------------------------------------------------


@pytest.mark.parametrize(
    "h,stride,pad,rows",
    [(8, 1, 1, 64), (10, 1, 0, 64), (16, 2, 1, 64), (8, 1, 0, 36)],
)
def test_conv2d_3x3_matches_the_reference(h, stride, pad, rows):
    c, f = 32, 16
    x = RNG.standard_normal((1, h, h, c)) * 0.4
    w = RNG.standard_normal((3, 3, c, f)) * 0.2
    if rows % 64:
        pytest.skip("a row count that does not fill the tile is a packer limit")
    got, want, prog = execute(
        lambda a, b: nn.conv2d(a, b, stride=stride, padding=pad),
        (spec(1, h, h, c), spec(3, 3, c, f)),
        (x, w),
        rows,
        c,
        f,
    )
    assert len(prog.windows) == 9, "one gathered operand per filter tap"
    assert rel(got, want) < TOL


def test_conv2d_1x1_emits_no_pad_at_all():
    """The pad was unconditional, so a 1x1 at padding 0 died on a border of
    width zero."""
    from ktpu.ir import OpKind

    g = D.trace(
        lambda a, b: nn.conv2d(a, b), spec(1, 8, 8, 32), spec(1, 1, 32, 16)
    )
    assert not any(op.kind is OpKind.PAD for op in g.ops)
    x = RNG.standard_normal((1, 8, 8, 32)) * 0.4
    w = RNG.standard_normal((1, 1, 32, 16)) * 0.2
    got, want, prog = execute(
        lambda a, b: nn.conv2d(a, b), (spec(1, 8, 8, 32), spec(1, 1, 32, 16)),
        (x, w), 64, 32, 16,
    )
    assert not prog.windows, "a 1x1 tap is the whole activation"
    assert rel(got, want) < TOL


def test_a_conv_on_a_computed_activation_says_what_is_missing():
    """The border exists only in the host's copy, so a value a band produced
    cannot be padded on the way into a FILL."""
    def fn(x, w):
        return nn.conv2d(x * 2.0, w, stride=1, padding=1)

    with pytest.raises(ScheduleError, match="PADDED"):
        lower(D.trace(fn, spec(1, 8, 8, 32), spec(3, 3, 32, 16)), T)


# ---- normalisation ---------------------------------------------------------


@pytest.mark.parametrize("width", [320, 640])
def test_layernorm_wider_than_one_pass_matches(width):
    x = RNG.standard_normal((64, width)) * 0.5
    g = RNG.standard_normal(width) * 0.1 + 1.0
    b = RNG.standard_normal(width) * 0.1
    got, want, _ = execute(
        layernorm, (spec(64, width), spec(width), spec(width)), (x, g, b),
        64, width, width,
    )
    assert rel(got, want) < TOL


def test_groupnorm32_on_an_nhwc_activation_matches():
    """The SDXL resblock's normaliser: 32 groups of 128 at C=64, HW=64."""
    c, hw, groups = 64, 64, 32
    x = RNG.standard_normal((1, 8, 8, c)) * 0.5
    gg = np.repeat(RNG.standard_normal(c) * 0.1 + 1.0, hw).reshape(groups, -1)
    bb = np.repeat(RNG.standard_normal(c) * 0.1, hw).reshape(groups, -1)
    cols = c // groups * hw
    got, want, _ = execute(
        lambda a, u, v: groupnorm(a, u, v, groups=groups),
        (spec(1, 8, 8, c), spec(groups, cols), spec(groups, cols)),
        (x, gg, bb), groups, cols, cols,
    )
    assert rel(got, want) < TOL


def test_groupnorm_rejects_a_group_count_that_does_not_divide():
    with pytest.raises(ValueError, match="does not divide"):
        D.trace(
            lambda a, u, v: groupnorm(a, u, v, groups=7),
            spec(1, 8, 8, 64), spec(32, 128), spec(32, 128),
        )


# ---- the feed-forward gate -------------------------------------------------


def test_geglu_is_one_projection_and_a_chunk():
    x = RNG.standard_normal((64, 128)) * 0.3
    w = RNG.standard_normal((128, 512)) * 0.2
    got, want, prog = execute(
        geglu, (spec(64, 128), spec(128, 512)), (x, w), 64, 128, 512
    )
    mm = [i for i in prog.insts if i.op.name == "GEMM"]
    assert mm, "one projection, not two"
    assert rel(got, want) < TOL


def test_a_geglu_chunk_that_splits_a_drained_tile_is_refused():
    """Half of a 128-wide projection is half a tile, which is not a run of
    addresses. The message has to say so rather than read the wrong half."""
    graph = D.trace(geglu, spec(64, 64), spec(64, 128))
    with pytest.raises(ValueError, match="splits a tile"):
        codegen(lower(graph, T), 64, 64, 128, T)


# ---- heads -----------------------------------------------------------------


def test_a_head_from_a_sliced_weight_matches():
    """`x @ w[:, h*dh:(h+1)*dh]` -- the window is on the WEIGHT, which the host
    packs, so the head split costs nothing at run time."""
    ell, dim, heads = 64, 256, 4
    dh = dim // heads
    x = RNG.standard_normal((ell, dim)) * 0.3
    w = RNG.standard_normal((dim, dim)) * 0.2
    for h in range(heads):
        got, want, prog = execute(
            lambda a, b, h=h: a @ b[:, h * dh : (h + 1) * dh],
            (spec(ell, dim), spec(dim, dim)), (x, w), ell, dim, dh,
        )
        assert len(prog.windows) == 1
        assert rel(got, want) < TOL


def test_project_heads_refuses_a_width_that_does_not_divide():
    with pytest.raises(ValueError, match="does not divide"):
        D.trace(lambda a, b: nn.project_heads(a, b, 7)[0], spec(64, 256), spec(256, 256))


def test_attention_over_a_device_projection_says_it_needs_a_relayout():
    """Q/K/V computed on the machine feed a GEMM as B, and only a host-packed
    operand is in L1-entry order."""
    def fn(x, wq, wk, wv):
        qs = nn.project_heads(x, wq, 2)
        ks = nn.project_heads(x, wk, 2)
        vs = nn.project_heads(x, wv, 2)
        return nn.multihead(qs, ks, vs, block=64)[0]

    with pytest.raises(ScheduleError, match="L1-entry order"):
        lower(D.trace(fn, *((spec(128, 128),) + (spec(128, 128),) * 3)), T)


def test_multihead_agrees_with_attention_on_the_same_heads():
    ell, dh, heads = 128, 64, 4
    qs, ks, vs = (
        [RNG.standard_normal((ell, dh)) * 0.3 for _ in range(heads)] for _ in range(3)
    )
    wo = RNG.standard_normal((heads * dh, heads * dh)) * 0.2

    def stacked(q, k, v, o):
        return nn.attention(q, k, v, block=64, wo=o)

    def listed(*a):
        return nn.multihead(
            list(a[:heads]), list(a[heads : 2 * heads]),
            list(a[2 * heads : 3 * heads]), block=64, wo=a[-1],
        )

    a_got, a_want, _ = execute(
        stacked,
        (spec(heads, ell, dh),) * 3 + (spec(heads * dh, heads * dh),),
        (np.stack(qs), np.stack(ks), np.stack(vs), wo),
        ell, dh, heads * dh,
    )
    m_got, m_want, _ = execute(
        listed,
        (spec(ell, dh),) * (3 * heads) + (spec(heads * dh, heads * dh),),
        (*qs, *ks, *vs, wo), ell, dh, heads * dh,
    )
    assert np.allclose(a_want, m_want)
    assert rel(a_got, a_want) < TOL and rel(m_got, m_want) < TOL


@pytest.mark.parametrize("outd", [64, 128, 256])
def test_an_output_projection_wider_than_one_tile_is_exact(outd):
    """`wo[h*dh:(h+1)*dh]` slices B on K, and B packs column-block MINOR: the
    offset moves by a block, not by k0*n. At 256 columns it was 128% wrong."""
    ell, dh, heads = 128, 64, 4
    q, k, v = (RNG.standard_normal((heads, ell, dh)) * 0.3 for _ in range(3))
    wo = RNG.standard_normal((heads * dh, outd)) * 0.2
    got, want, _ = execute(
        lambda a, b, c, o: nn.attention(a, b, c, block=64, wo=o),
        (spec(heads, ell, dh),) * 3 + (spec(heads * dh, outd),),
        (q, k, v, wo), ell, dh, outd,
    )
    assert rel(got, want) < TOL


# ---- upsample --------------------------------------------------------------


def test_nearest_2x_upsample_matches_numpy():
    n, h, c = 1, 8, 32
    x = RNG.standard_normal((n, h, h, c)) * 0.4
    dup = nearest2x_matrix(h, h)
    got, want, _ = execute(
        upsample_nearest2x, (spec(n, h, h, c), spec(4 * h * h, h * h)),
        (x, dup), 4 * h * h, h * h, c,
    )
    assert rel(got, want) < TOL
    assert np.allclose(want, np.repeat(np.repeat(x, 2, axis=1), 2, axis=2))


def test_an_upsample_made_only_of_views_fails_loudly():
    """reshape + expand + reshape moves no data and computes nothing; it used
    to lower to ZERO bands and report success."""
    def fn(x):
        n, h, w, c = x.shape
        v = x.reshape(n, h, 1, w, 1, c).expand(n, h, 2, w, 2, c)
        return v.reshape(n, 2 * h, 2 * w, c)

    with pytest.raises(ScheduleError, match="view"):
        lower(D.trace(fn, spec(1, 8, 8, 32)), T)
