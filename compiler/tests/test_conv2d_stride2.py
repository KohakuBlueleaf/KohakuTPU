"""3x3 stride 2 and the nearest-2x upsample, on the mover's im2col.

SDXL's two `Downsample2D` are the only strided convolutions it has. A stride is
two factors in the im2col walk -- the lane step and the output-row step -- so
only the outputs that exist are gathered: the alternative, computing every
output and discarding three in four, is 4.0x the MAC.

`Upsample2D` is nearest-2x then a 3x3, and the 2x activation need not exist: a
tap of the upsampled convolution takes one of two input rows, so each output
residue class is a 2x2 convolution over the ORIGINAL image at a shifted origin.
"""

import numpy as np
import pytest
from kohakutpu.model import SimDevice
from kohakutpu.ops.conv2d import Geometry

from kohakutpu import ops as O

FP16 = np.float16


def rel(got, want) -> np.ndarray:
    want = np.asarray(want, np.float64)
    return np.abs(np.asarray(got, np.float64) - want) / np.abs(want).max()


def ref_conv(x, k, stride):
    """`[H][W][C]` against `[N][C][3][3]`, pad 1, in float64."""
    h, w, c = x.shape
    pad = np.zeros((h + 2, w + 2, c), np.float64)
    pad[1:-1, 1:-1, :] = np.float64(x)
    hs, ws = -(-h // stride), -(-w // stride)
    out = np.zeros((hs, ws, k.shape[0]))
    for dy in range(3):
        for dx in range(3):
            win = pad[dy : dy + h : stride, dx : dx + w : stride, :][:hs, :ws, :]
            out[: win.shape[0], : win.shape[1], :] += (
                win @ np.float64(k[:, :, dy, dx]).T
            )
    return out


def randn(shape, seed, scale=0.3):
    return np.asarray(np.random.default_rng(seed).standard_normal(shape) * scale, FP16)


# ----------------------------------------------------------- the convolution
@pytest.mark.parametrize(
    "h,w,cin,cout,gm,gn",
    [(16, 16, 32, 32, 8, 8), (32, 32, 32, 64, 8, 8), (64, 64, 32, 32, 16, 16)],
)
def test_conv2d_stride2_is_one_matmul_and_no_relayout(h, w, cin, cout, gm, gn):
    x, k = randn((h, w, cin), 3), randn((cout, cin, 3, 3), 4, scale=0.2)
    hs, ws = -(-h // 2), -(-w // 2)
    dev = SimDevice(size=2048 << 20)
    before = dev.counters.copy()
    got = O.conv2d_stride2(
        dev.tensor(x), dev.tensor(O.weights_for_k(k, cin)), gm=gm, gn=gn
    ).numpy()
    rows = O.positions(h, w, 2)
    picked = np.stack([got[r] for r, _, _ in rows]).reshape(hs, ws, cout)

    err = rel(picked, ref_conv(x, k, 2))
    # The grade a stride-1 conv and a plain matmul both return at this depth.
    assert float(np.percentile(err, 99)) < 2e-2
    assert float((err > 0.10).mean()) == 0.0
    assert dev.counters.get("dispatches", 0) - before.get("dispatches", 0) == 1
    assert dev.counters.get("relayouts", 0) - before.get("relayouts", 0) == 0
    assert dev.saturated == 0


@pytest.mark.parametrize("h,w,c", [(32, 32, 32), (64, 64, 320), (9, 7, 64)])
def test_the_stride_gathers_only_the_outputs_that_exist(h, w, c):
    """The im2col of a stride-2 conv is a quarter of the dense one's rows."""
    dense, strided = Geometry(h, w, c), Geometry(h, w, c, stride=2)
    assert strided.oh == -(-h // 2) and strided.ow == -(-w // 2)
    assert 3.0 < dense.rows / strided.rows <= 4.0


def test_positions_reads_the_same_for_both_strides():
    assert O.positions(8, 8)[:3] == [(0, 0, 0), (1, 0, 1), (2, 0, 2)]
    assert O.positions(8, 8, 2)[:3] == [(0, 0, 0), (1, 0, 1), (2, 0, 2)]
    assert len(O.positions(9, 7, 2)) == 5 * 4


# ------------------------------------------ the upsample, which is the same idea
def ref_upconv(x, k):
    """nearest-2x then a 3x3 pad-1 conv, in float64. `[2H][2W][N]`."""
    up = np.repeat(np.repeat(np.float64(x), 2, axis=0), 2, axis=1)
    h, w, c = up.shape
    pad = np.zeros((h + 2, w + 2, c))
    pad[1:-1, 1:-1, :] = up
    out = np.zeros((h, w, k.shape[0]))
    for dy in range(3):
        for dx in range(3):
            out += pad[dy : dy + h, dx : dx + w, :] @ np.float64(k[:, :, dy, dx]).T
    return out


def upsampled(dev, x, k, cin, gm, gn):
    """The four residue classes, interleaved back into `[2H][2W][N]`."""
    h, w = x.shape[0], x.shape[1]
    folded = O.weights_for_upsample2(k, cin)
    out = np.zeros((2 * h, 2 * w, k.shape[0]))
    rows = O.positions(h, w)
    for iy in range(2):
        for ix in range(2):
            part = O.conv2d_upsample2(
                dev.tensor(x),
                dev.tensor(folded[iy * 2 + ix]),
                iy=iy,
                ix=ix,
                gm=gm,
                gn=gn,
            ).numpy()
            out[iy::2, ix::2, :] = np.stack([part[r] for r, _, _ in rows]).reshape(
                h, w, k.shape[0]
            )
    return out


@pytest.mark.parametrize(
    "h,w,cin,cout,gm,gn",
    [(8, 8, 32, 32, 8, 8), (16, 16, 32, 64, 8, 8), (8, 6, 20, 16, 4, 4)],
)
def test_the_upsample_folds_into_the_TAPS_and_is_never_materialised(
    h, w, cin, cout, gm, gn
):
    """16 MAC per input pixel against 36, and a quarter of the activation."""
    x, k = randn((h, w, cin), 10), randn((cout, cin, 3, 3), 11, scale=0.2)
    dev = SimDevice(size=2048 << 20)
    got = upsampled(dev, x, k, cin, gm, gn)
    err = rel(got, ref_upconv(x, k))
    assert float(np.percentile(err, 99)) < 2e-2
    assert float((err > 0.10).mean()) == 0.0
    assert dev.counters.get("relayouts", 0) == 0
    assert dev.saturated == 0


def test_the_folded_weights_are_the_ORIGINAL_taps_summed():
    """Exact, because a fold is an addition of weights and nothing else.

    The trap is the tap index: the input offset runs -1..1 and the operand's
    runs 0..1, and the difference is exactly the class shift the walk adds
    back. Off by one there reads the neighbouring pixel and still looks like a
    convolution.
    """
    cin = 32
    k = randn((8, cin, 3, 3), 12, scale=1.0)
    folded = O.weights_for_upsample2(k, cin)
    assert folded.shape == (4, 8, 4 * cin)
    for cls in range(4):
        got = folded[cls].reshape(8, 4, cin).sum(1)
        assert np.allclose(np.float64(got), np.float64(k).sum((2, 3)), atol=1e-2)
    a = np.float64(folded[0].reshape(8, 2, 2, cin))
    assert np.allclose(a[:, 0, 0], np.float64(k[:, :, 0, 0]), atol=1e-2)
    b = np.float64(folded[3].reshape(8, 2, 2, cin))
    assert np.allclose(b[:, 1, 1], np.float64(k[:, :, 2, 2]), atol=1e-2)


def test_the_upsample_beats_materialising_by_the_MAC_ratio():
    """Four 2x2 classes over the original rows against one 3x3 over 4x them."""
    for h, w, c, least in ((8, 8, 32, 1.9), (32, 32, 64, 2.0)):
        fused = 4 * Geometry(h, w, c, kh=2, kw=2, out=(h, w)).rows * 4
        dense = Geometry(2 * h, 2 * w, c).rows * 9
        assert dense / fused >= least
