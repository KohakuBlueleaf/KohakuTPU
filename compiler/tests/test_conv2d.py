"""Convolution as an on-card im2col (the mover) and an ordinary matmul.

`ops.conv2d` writes the patch matrix with converting MOVER steps -- the walk
gathers each 3x3 window, the transform slot quantises on the way -- and runs
`ops.matmul` on it. Checked against a float64 convolution on the unit models,
whose mover runs the same moves (`model.run_move`). The im2col operand itself
is checked BYTE FOR BYTE against an independent reference: the patch matrix
built in numpy and packed by `MxEntry.pack`.
"""

import numpy as np
import pytest
from kohakuaccel.package import mover as PM
from kohakutpu.kernels import conv2d_bias
from kohakutpu.model import SimDevice, run_move
from kohakutpu.ops.conv2d import Geometry, im2col_moves

from kohakutpu import layout as LO
from kohakutpu import ops as O

FP16 = np.float16


def randn(shape, seed, scale=0.5):
    return np.asarray(np.random.default_rng(seed).standard_normal(shape) * scale, FP16)


def ref_conv(x, k, stride=1):
    """`[H][W][C]` against `[N][C][3][3]`, pad 1, in float64. ``[OH][OW][N]``."""
    h, w, _ = x.shape
    xp = np.pad(np.float64(x), ((1, 1), (1, 1), (0, 0)))
    oh, ow = (h - 1) // stride + 1, (w - 1) // stride + 1
    out = np.zeros((oh, ow, k.shape[0]))
    for dy in range(3):
        for dx in range(3):
            win = xp[dy : dy + stride * oh : stride, dx : dx + stride * ow : stride]
            out += win @ np.float64(k[:, :, dy, dx]).T
    return out


def picked(held, h, w, cout, stride=1):
    oh, ow = (h - 1) // stride + 1, (w - 1) // stride + 1
    out = np.zeros((oh, ow, cout))
    for row, y, x in O.positions(h, w, stride):
        out[y, x] = held[row, :cout]
    return out


def run(h, w, cin, cout, stride=1, gm=16, gn=32, seed=3):
    x, k = randn((h, w, cin), seed), randn((cout, cin, 3, 3), seed + 1, 0.2)
    dev = SimDevice(size=1 << 27)
    fn = O.conv2d if stride == 1 else O.conv2d_stride2
    out = fn(dev.tensor(x), dev.tensor(O.weights_for_k(k, cin)), gm=gm, gn=gn)
    got = picked(out.numpy(), h, w, cout, stride)
    want = ref_conv(x, k, stride)
    return got, want, dev


@pytest.mark.parametrize(
    "h,w,cin,cout,stride,gm,gn",
    [
        (8, 8, 32, 32, 1, 4, 8),
        (12, 10, 64, 64, 1, 16, 16),
        (16, 16, 96, 32, 1, 8, 8),
        (12, 10, 40, 24, 1, 16, 32),  # C_in not a multiple of 32: padded to 64
        (16, 16, 32, 32, 2, 8, 8),
        (32, 32, 64, 64, 2, 8, 8),
        (9, 7, 20, 16, 2, 16, 32),  # odd extent, ragged C_in, stride 2
    ],
)
def test_conv2d_matches_a_direct_convolution(h, w, cin, cout, stride, gm, gn):
    """1.6e-2 is this machine's MXFP7 floor on a contraction this long."""
    got, want, dev = run(h, w, cin, cout, stride, gm, gn)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05
    assert dev.saturated == 0
    assert dev.counters.get("im2col") == 1
    # The weights once, and nothing else quantised: the patches were born MXFP7.
    assert dev.counters.get("quantised") == 1
    assert dev.counters.get("relayouts", 0) == 0


@pytest.mark.parametrize(
    "h,w,cin,stride,gm", [(8, 8, 32, 1, 2), (12, 10, 40, 1, 16), (9, 7, 20, 2, 16)]
)
def test_the_im2col_is_byte_identical_to_the_packed_patch_matrix(h, w, cin, stride, gm):
    """The mover's walk against numpy's: im2col in float, packed by MxEntry.pack."""
    x = randn((h, w, cin), 7)
    g = Geometry(h, w, cin, stride=stride)
    gt = g.groups(gm)
    img = g.image()
    dev = SimDevice(size=1 << 26)
    src = dev.put(x, img).addr
    lay = LO.MxEntry(gt, 1, 0)
    dst = dev.alloc(lay.nbytes((g.rows, g.k)))
    for writes in im2col_moves(g, gt, src, dst):
        run_move(writes, dev.card.mem)
    got = dev.read(dst, lay.nbytes((g.rows, g.k)))

    xp = np.zeros((img.hp, img.wp, g.cp), FP16)
    xp[1 : 1 + h, 1 : 1 + w, :cin] = x
    patches = np.zeros((g.rows, g.k), FP16)
    for oy in range(g.oh):
        for ox in range(g.ow4):
            for t in range(9):
                dy, dx = divmod(t, 3)
                pix = xp[oy * stride + dy, ox * stride + dx]
                patches[oy * g.ow4 + ox, t * g.cp : (t + 1) * g.cp] = pix
    assert got == lay.pack(patches)


def test_a_3x3_on_a_wide_layer_is_a_handful_of_moves():
    """Eight walk levels against six: the widest four go to the hardware.

    128x128x320 at 16 lane groups a tile: output row 128, group 16, channel
    block 10 and tap row 3 are walked; tile 2 x tap column 3 are six moves.
    """
    g = Geometry(128, 128, 320)
    gt = g.groups(16)
    moves = im2col_moves(g, gt, 0x1000_0000, 0x2000_0000)
    assert len(moves) == 6, len(moves)
    assert len(im2col_moves(g, g.groups(32), 0x1000_0000, 0x2000_0000)) == 3
    for writes in moves:
        dims = [v for r, v in writes if r == PM.R_DIM]
        assert len(dims) <= 2 * PM.NDIM


def test_a_weight_is_quantised_once_across_two_convolutions():
    x, k = randn((8, 8, 32), 1), randn((32, 32, 3, 3), 2, 0.2)
    dev = SimDevice(size=1 << 26)
    wt = dev.tensor(O.weights_for_k(k, 32))
    O.conv2d(dev.tensor(x), wt, gm=4, gn=8).numpy()
    O.conv2d(dev.tensor(randn((8, 8, 32), 9)), wt, gm=4, gn=8).numpy()
    assert dev.counters.get("quantised") == 1


def test_an_activation_a_kernel_produced_is_padded_by_the_mover():
    """A Flat ``[H][W][C]`` result: a FILL of zeros and one COPY, no host."""
    a, b = randn((8, 8, 32), 4), randn((8, 8, 32), 5)
    k = randn((32, 32, 3, 3), 6, 0.2)
    dev = SimDevice(size=1 << 26)
    s = O.residual(dev.tensor(a), dev.tensor(b))
    out = O.conv2d(s, dev.tensor(O.weights_for_k(k, 32)), gm=4, gn=8)
    got = picked(out.numpy(), 8, 8, 32)
    want = ref_conv(np.asarray(np.float64(a) + np.float64(b), FP16), k)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05
    assert dev.counters.get("relayouts", 0) == 0


def test_conv2d_bias_adds_the_bias_per_row():
    x, k = randn((8, 8, 32), 1), randn((32, 32, 3, 3), 2, 0.2)
    dev = SimDevice(size=1 << 26)
    rows = Geometry(8, 8, 32).rows
    bias = randn((rows, 32), 3, 0.1)
    out = conv2d_bias(
        dev.tensor(x), dev.tensor(O.weights_for_k(k, 32)), dev.tensor(bias)
    )
    got = picked(out.numpy() - np.float64(bias), 8, 8, 32)
    want = ref_conv(x, k)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


def test_weights_for_k_pads_the_channels_with_zeros():
    k = randn((8, 40, 3, 3), 1, 1.0)
    b = O.weights_for_k(k, 40)
    assert b.shape == (8, 9 * 64)
    blocks = b.reshape(8, 9, 64)
    assert not blocks[:, :, 40:].any()
    assert np.array_equal(blocks[:, 4, :40], k[:, :, 1, 1])


def test_positions_are_row_major_over_whole_lanes():
    assert O.positions(8, 8)[:3] == [(0, 0, 0), (1, 0, 1), (2, 0, 2)]
    assert O.positions(8, 10)[10] == (12, 1, 0)  # a 10-wide row is 12 lanes
    assert len(O.positions(9, 7, 2)) == 5 * 4
