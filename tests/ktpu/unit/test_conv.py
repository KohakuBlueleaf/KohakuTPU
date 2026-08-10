"""conv2d/conv1d read as accumulating matmuls rather than as an im2col.

The numeric tests are against a direct convolution. The structural test is the
one that matters for the design: no intermediate is ever KH*KW times the
operand, which is what separates this from an im2col and is the reason it needs
no memory mover.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.dsl.nn import conv1d, conv2d
from ktpu.interp import run_one
from ktpu.ir import FP16

RNG = np.random.default_rng(7)


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def np_conv2d(x, w, stride, padding):
    n, h, wd, _ = x.shape
    kh, kw, _, f = w.shape
    sh, sw = stride
    ph, pw = padding
    xp = np.pad(x, ((0, 0), (ph, ph), (pw, pw), (0, 0)))
    ho = (h + 2 * ph - kh) // sh + 1
    wo = (wd + 2 * pw - kw) // sw + 1
    out = np.zeros((n, ho, wo, f))
    for y in range(ho):
        for x0 in range(wo):
            patch = xp[:, y * sh : y * sh + kh, x0 * sw : x0 * sw + kw, :]
            out[:, y, x0, :] = np.tensordot(patch, w, axes=([1, 2, 3], [0, 1, 2]))
    return out


@pytest.mark.parametrize(
    "kh,kw,stride,padding",
    [
        (3, 3, 1, 1),  # the SDXL ResBlock case
        (1, 1, 1, 0),  # projection
        (3, 3, 2, 1),  # downsample
        (5, 5, 1, 2),
        (3, 3, 1, 0),  # valid
        (2, 4, (2, 1), (0, 2)),  # asymmetric everything
    ],
)
def test_conv2d_matches_a_direct_convolution(kh, kw, stride, padding):
    n, h, w, c, f = 2, 8, 9, 4, 6
    st = stride if isinstance(stride, tuple) else (stride, stride)
    pd = padding if isinstance(padding, tuple) else (padding, padding)
    g = D.trace(
        conv2d, spec(n, h, w, c), spec(kh, kw, c, f), stride=stride, padding=padding
    )
    xa = RNG.normal(size=(n, h, w, c))
    wa = RNG.normal(size=(kh, kw, c, f))
    got = run_one(g, xa, wa)
    assert np.allclose(got, np_conv2d(xa, wa, st, pd), rtol=1e-9, atol=1e-12)


def test_conv1d_matches_conv2d_with_unit_height():
    n, ell, c, f, k = 2, 12, 4, 5, 3
    g = D.trace(conv1d, spec(n, ell, c), spec(k, c, f), stride=1, padding=1)
    xa = RNG.normal(size=(n, ell, c))
    wa = RNG.normal(size=(k, c, f))
    got = run_one(g, xa, wa)
    want = np_conv2d(xa[:, None], wa[None], (1, 1), (0, 1))
    assert np.allclose(got, want[:, 0], rtol=1e-9, atol=1e-12)


def test_conv2d_never_materialises_an_im2col():
    """An im2col would be KH*KW wider than any value this graph holds."""
    n, h, w, c, f, kh, kw = 2, 8, 8, 4, 6, 3, 3
    g = D.trace(conv2d, spec(n, h, w, c), spec(kh, kw, c, f), stride=1, padding=1)
    widest = max(op.out.numel for op in g.ops)
    im2col = n * h * w * c * kh * kw
    assert widest < im2col
    # the padded operand is the largest thing there is
    assert widest <= n * (h + 2) * (w + 2) * max(c, f)


def test_conv2d_emits_one_matmul_per_filter_tap():
    from ktpu.ir import OpKind

    n, h, w, c, f = 1, 6, 6, 3, 3
    for kh, kw in ((1, 1), (3, 3), (2, 5)):
        g = D.trace(conv2d, spec(n, h, w, c), spec(kh, kw, c, f), padding=0)
        got = sum(op.kind is OpKind.MATMUL for op in g.ops)
        assert got == kh * kw


def test_conv2d_rejects_a_channel_mismatch():
    with pytest.raises(ValueError, match="channel mismatch"):
        D.trace(conv2d, spec(1, 8, 8, 4), spec(3, 3, 5, 6))


def test_conv2d_rejects_a_filter_larger_than_the_padded_input():
    with pytest.raises(ValueError, match="output would be"):
        D.trace(conv2d, spec(1, 4, 4, 2), spec(7, 7, 2, 2), padding=0)


def test_conv2d_rejects_the_wrong_rank():
    with pytest.raises(ValueError, match=r"expects \(N, H, W, C\)"):
        D.trace(conv2d, spec(8, 8, 4), spec(3, 3, 4, 6))
