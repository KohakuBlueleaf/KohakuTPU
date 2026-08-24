"""3x3 stride 2, and the two rearrangements `ConvEntry` already gives away free.

SDXL's two `Downsample2D` are the only strided convolutions it has, and the
alternative to a residue packer is computing every output and discarding three
in four: **4.0x the MAC**, at every shape. The packer is `conv2d.md` 4.1 --
split BOTH axes by residue and the tap offset is a constant again:

    dy = qy*s + ry,  dx = qx*s + rx
    A[s*oy + dy, s*ox + dx] == sub[ry, rx][oy + qy, ox + qx]

Nothing else changes: the same sweep, the same nine taps, the same one dispatch.
"""

import numpy as np
import pytest
from kohakutpu.model import SimDevice

from kohakutpu import layout as LO
from kohakutpu import ops as O

FP16 = np.float16
KB = LO.KBLOCK


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


# --------------------------------------------------------------- the packer
@pytest.mark.parametrize(
    "h,w,c,step", [(8, 8, 32, 2), (9, 7, 64, 2), (32, 32, 32, 2), (16, 16, 32, 1)]
)
def test_the_residue_packer_round_trips_BIT_EXACT(h, w, c, step):
    """It only reorders and zero-fills, so an inexact round trip is a lost pixel.

    An odd extent is here because `9x7` at stride 2 gives sub-planes of three
    different shapes, and a packer sized from the first one drops the rest.
    """
    x = randn((h, w, c), 1, scale=1.0)
    lay = LO.ConvEntry(1, 8, step)
    back = lay.unpack(lay.pack(x), (h, w, c))
    assert np.array_equal(np.asarray(back, FP16), x)


def test_stride_1_is_the_layout_it_ALWAYS_WAS():
    """The additive rule: `step=1` must be invisible to everything already built."""
    x = randn((16, 16, 64), 2, scale=1.0)
    plain, stepped = LO.ConvEntry(1, 8), LO.ConvEntry(1, 8, 1)
    assert plain.key == stepped.key
    assert plain.pack(x) == stepped.pack(x)
    assert plain.nbytes(x.shape) == stepped.nbytes(x.shape)


def test_the_step_is_NOT_called_stride_and_the_reason_is_load_bearing():
    """`lang.backend._held` reads `.stride` off a layout as a BATCH BYTE STRIDE.

    A `stride=2` field there sizes the whole activation at ONE element, and the
    refusal that follows names a tail-padding problem that does not exist. Cost:
    a compile that reported `a fill of 'a' reaches byte 21248 of 2`.
    """
    assert not hasattr(LO.ConvEntry(1, 8, 2), "stride")
    assert LO.ConvEntry(1, 8, 2).step == 2


# ----------------------------------------------------------- the convolution
@pytest.mark.parametrize(
    "h,w,cin,cout,gm,gn",
    [(16, 16, 32, 32, 8, 8), (32, 32, 32, 64, 8, 8), (64, 64, 32, 32, 16, 16)],
)
def test_conv2d_stride2_is_one_dispatch_and_no_relayout(h, w, cin, cout, gm, gn):
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


def test_the_packer_beats_dense_and_discard_by_the_MAC_RATIO():
    """4.0x of the arithmetic is discarded; the cost model sees 3.7-4.0x of it."""
    from kohakutpu.cost import time as cost_time

    for h, w, cin, cout, gm, gn in ((32, 32, 32, 64, 8, 8), (64, 64, 32, 32, 16, 16)):
        x, k = randn((h, w, cin), 5), randn((cout, cin, 3, 3), 6, scale=0.2)
        b = O.weights_for_k(k, cin)
        dev = SimDevice(size=2048 << 20)
        strided = cost_time(
            O.conv2d_stride2.plan(dev.tensor(x), dev.tensor(b), gm=gm, gn=gn),
            dev.machine,
        )
        dense = cost_time(
            O.conv2d.plan(dev.tensor(x), dev.tensor(b), gm=gm, gn=gn), dev.machine
        )
        assert 3.5 < dense.cycles / strided.cycles <= 4.0


def test_positions_reads_the_same_for_both_strides():
    """The centre tap is at `wp + 1` either way, which is what makes that true."""
    assert O.positions(8, 8)[:3] == [(0, 0, 0), (1, 0, 1), (2, 0, 2)]
    assert O.positions(8, 8, 2)[:3] == [(0, 0, 0), (1, 0, 1), (2, 0, 2)]
    assert len(O.positions(9, 7, 2)) == 5 * 4


# ------------------------------- concat and slice, which are neither of those
CONCATS = [
    (32, 32, 1280, 1280),
    (64, 64, 640, 640),
    (128, 128, 320, 320),
    (64, 64, 640, 1280),
    (8, 8, 32, 64),
]


def body(lay, arr) -> bytes:
    """`pack` without the tail entries -- what an ADJACENT write omits."""
    _, _, tail = lay.geometry(arr.shape)
    raw = lay.pack(arr)
    return raw[: len(raw) - tail * LO.LANES * KB * 2]


@pytest.mark.parametrize("h,w,ca,cb", CONCATS)
def test_a_channel_CONCAT_is_two_buffers_written_adjacently(h, w, ca, cb):
    """`torch.cat([h, hs.pop()], dim=1)`, 9 per UNet forward, is an ALLOCATION.

    In `[C/32][plane][32]` the channel block is the outer axis, so the bytes of
    the concatenation ARE the bytes of the two operands in order -- provided the
    first is written without its tail, which is one span rather than two.
    """
    a, b = randn((h, w, ca), 7, 1.0), randn((h, w, cb), 8, 1.0)
    lay = LO.ConvEntry(1, 8)
    assert body(lay, a) + lay.pack(b) == lay.pack(np.concatenate([a, b], axis=2))


@pytest.mark.parametrize("h,w,ca,cb", CONCATS)
def test_a_channel_SLICE_of_the_low_blocks_is_the_LEADING_PREFIX(h, w, ca, cb):
    both = randn((h, w, ca + cb), 9, 1.0)
    lay = LO.ConvEntry(1, 8)
    front = body(lay, both[:, :, :ca])
    assert lay.pack(both)[: len(front)] == front


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
    "h,w,cin,cout,gm,gn", [(8, 8, 32, 32, 8, 8), (16, 16, 32, 64, 8, 8)]
)
def test_the_upsample_folds_into_the_TAPS_and_is_never_materialised(
    h, w, cin, cout, gm, gn
):
    """`Upsample2D` is nearest-2x then a 3x3, and the 2x activation need not exist.

    A tap of the upsampled convolution takes one of two input rows, so each
    output residue class is a 2x2 convolution over the ORIGINAL plane. 16 MAC
    per input pixel against 36, and a quarter of the activation.
    """
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
    runs 0..1, and the difference is exactly the class shift the kernel adds
    back. Off by one there reads the neighbouring pixel and still looks like a
    convolution.
    """
    cin = 32
    k = randn((8, cin, 3, 3), 12, scale=1.0)
    folded = O.weights_for_upsample2(k, cin)
    assert folded.shape == (4, 8, 4 * cin)
    # Every original tap lands exactly once in each class, so each class's
    # weights sum to the whole 3x3 -- a dropped tap shows here and nowhere else.
    for cls in range(4):
        got = folded[cls].reshape(8, 4, cin).sum(1)
        assert np.allclose(np.float64(got), np.float64(k).sum((2, 3)), atol=1e-2)
    # iy=0 pairs dy 1 and 2 into tap row 1; iy=1 pairs dy 0 and 1 into tap row 0.
    a = np.float64(folded[0].reshape(8, 2, 2, cin))
    assert np.allclose(a[:, 0, 0], np.float64(k[:, :, 0, 0]), atol=1e-2)
    b = np.float64(folded[3].reshape(8, 2, 2, cin))
    assert np.allclose(b[:, 1, 1], np.float64(k[:, :, 2, 2]), atol=1e-2)


def test_the_upsample_beats_materialising_at_a_REAL_plane_and_not_a_tiny_one():
    """2.0x at 16x16 and above; 1.08x at 8x8, where the pad ring is the layer."""
    from kohakutpu.cost import time as cost_time

    for h, w, cin, cout, gm, gn, least in (
        (8, 8, 32, 32, 8, 8, 1.0),
        (32, 32, 64, 32, 16, 16, 1.9),
    ):
        x, k = randn((h, w, cin), 13), randn((cout, cin, 3, 3), 14, scale=0.2)
        folded = O.weights_for_upsample2(k, cin)
        dev = SimDevice(size=4096 << 20)
        fused = sum(
            cost_time(
                O.conv2d_upsample2.plan(
                    dev.tensor(x),
                    dev.tensor(folded[c]),
                    iy=c // 2,
                    ix=c % 2,
                    gm=gm,
                    gn=gn,
                ),
                dev.machine,
            ).cycles
            for c in range(4)
        )
        up = np.asarray(np.repeat(np.repeat(x, 2, axis=0), 2, axis=1), FP16)
        dense = cost_time(
            O.conv2d.plan(
                dev.tensor(up), dev.tensor(O.weights_for_k(k, cin)), gm=gm, gn=gn
            ),
            dev.machine,
        ).cycles
        assert dense / fused >= least
