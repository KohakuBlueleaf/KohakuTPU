"""3x3 conv as a matmul over `[C/32][plane][32]`, on the current bitstream.

Branch C: store the activation channel-block-major, run
with `nk = 1`, and a tap becomes a constant added to the FILL address. Nothing
in the hardware changes.

**An ISA-level harness, and it stays one now that `kernels.conv2d` exists.**
It computes its own fill addresses, so it is an independent check on the DSL's:
`test_the_dsl_kernel_emits_the_same_program_this_harness_does` runs both and
compares. It also reaches shapes and tile counts the kernel would take an
afternoon to compile, which is what the SDXL rows below need.
"""

from dataclasses import dataclass

import numpy as np
import pytest
from kohakutpu.hw import mxfp7
from kohakutpu.hw import tensor as T
from kohakutpu.isa import ISA
from kohakutpu.model import ARENA_BASE as MODEL_BASE
from kohakutpu.model import SimDevice

from kohakutpu import layout as LO

#: One 32-channel block of one pixel, in bytes: the tap stride, and a quarter of
#: an L1 entry -- which is why `Slice` had to grow a lane offset.
PIXEL_BYTES = T.KBLOCK * 2

#: The boundary an AXI burst may not cross. `mag_mem_port.v` has no split logic
#: -- no `4096` appears in the file -- and the model reads memory flat.
AXI_PAGE = 4096

TAPS = tuple((dy, dx) for dy in range(3) for dx in range(3))


@dataclass(frozen=True)
class Conv:
    """One 3x3 layer as branch C schedules it.

    `gm` lane groups of 4 output positions and `gn` of 4 output channels per
    pass; `nk` is 1 and cannot be anything else, because two channel blocks of
    one pixel are `plane*64` bytes apart and a fill of `nk` blocks is one run.
    """

    h: int
    w: int
    cin: int
    cout: int
    gm: int = 8
    gn: int = 8
    pad: int = 1

    @property
    def geometry(self) -> tuple:
        return T.conv_geometry(self.h, self.w, self.pad)

    @property
    def plane(self) -> int:
        """Positions one 32-channel block occupies, rounded to whole entries."""
        return self.geometry[2]

    @property
    def steps(self) -> int:
        """Sweep steps: one per (tap, channel block), which is K/32 at nk=1."""
        return len(TAPS) * (self.cin // T.KBLOCK)

    @property
    def useful(self) -> int:
        """Flat positions carrying a wanted output: the last is `(H-1)*Wp+W-1`."""
        _, wp, _ = self.geometry
        return (self.h - 1) * wp + self.w

    @property
    def tiles_m(self) -> int:
        return -(-self.useful // (self.gm * T.LANES))

    @property
    def tiles_n(self) -> int:
        return -(-self.cout // (self.gn * T.LANES))

    @property
    def tail(self) -> int:
        """Entries of zeros the allocation needs past the last channel block.

        The last M tile reads a tap past its own positions. From the last USEFUL
        position that tap lands exactly on the plane's last position, so this is
        only what rounding the sweep up to whole tiles adds.
        """
        _, wp, plane = self.geometry
        over = self.tiles_m * self.gm * T.LANES + 2 * wp + 2 - plane
        return max(0, -(-over // T.LANES))

    @property
    def flits_per_pass(self) -> int:
        """FILL A, FILL B, GEMM per sweep step, and one DRAIN for the tile."""
        return 3 * self.steps + 1

    @property
    def passes(self) -> int:
        return self.tiles_m * self.tiles_n

    def rows(self, tiles) -> int:
        return len(tiles) * self.gm * T.LANES


def weights_for_k(kernel, cin: int) -> np.ndarray:
    """PyTorch's `(out, in, kh, kw)` as the B operand `[N][9C]` this K sweep wants.

    K runs (tap, channel block, channel) because the sweep does: step `s` is tap
    ``s // (C/32)`` at block ``s % (C/32)``. A host-side permute, done once.
    """
    cout = kernel.shape[0]
    out = np.zeros((cout, len(TAPS) * cin), np.float16)
    for t, (dy, dx) in enumerate(TAPS):
        out[:, t * cin : (t + 1) * cin] = kernel[:, :, dy, dx]
    return out


def reference(x, kernel, pad: int = 1) -> np.ndarray:
    """Direct convolution in float64. `x` is HWC, `kernel` is `(out, in, kh, kw)`."""
    h, w, _ = x.shape
    xp = np.pad(np.asarray(x, np.float64), ((pad, pad), (pad, pad), (0, 0)))
    k = np.asarray(kernel, np.float64)
    out = np.zeros((h, w, k.shape[0]))
    for dy, dx in TAPS:
        out += np.tensordot(xp[dy : dy + h, dx : dx + w, :], k[:, :, dy, dx], (2, 1))
    return out


def quantised(conv: Conv, x, kernel, tiles) -> np.ndarray:
    """The same convolution as a materialised im2col through MXFP7, as `[H][W][N]`.

    The gap to this is the ADDRESSING; the gap between this and float64 is what
    MXFP7 costs. Reporting only machine-versus-float64 adds the two together and
    calls the sum a tolerance, which is how a real fault hides in the noise.
    """
    _, wp, _ = conv.geometry
    xp = np.pad(np.asarray(x, np.float16), ((conv.pad, conv.pad),) * 2 + ((0, 0),))
    flat = xp.reshape(-1, conv.cin)
    per = conv.gm * T.LANES
    rows = np.concatenate([np.arange(t * per, (t + 1) * per) for t in tiles])
    a = np.zeros((rows.size, len(TAPS) * conv.cin), np.float16)
    for t, (dy, dx) in enumerate(TAPS):
        at = rows + dy * wp + dx
        keep = at < flat.shape[0]
        a[keep, t * conv.cin : (t + 1) * conv.cin] = flat[at[keep]]
    got = mxfp7.model_matmul(a, weights_for_k(np.asarray(kernel, np.float16), conv.cin))

    out = np.zeros((conv.h, conv.w, conv.cout))
    for n, q in enumerate(rows):
        y, x0 = divmod(int(q), wp)
        if y < conv.h and x0 < conv.w:
            out[y, x0] = got[n]
    return out


def flits(conv: Conv, a: int, b: int, c: int, tile_m: int, at: int, j: int) -> list:
    """One pass: the nine taps' fills, their sweeps, and the drain.

    `tile_m` is the tile's place in the sweep and `at` its place in the result,
    which differ when only some tiles are run. Banks alternate per step: a GEMM
    retires when its sweep starts, so the next fill must land elsewhere.
    """
    _, wp, plane = conv.geometry
    blocks = conv.cin // T.KBLOCK
    out: list = []
    for s in range(conv.steps):
        dy, dx = TAPS[s // blocks]
        cb = s % blocks
        bank = s % 2
        base = (cb * plane + tile_m * conv.gm * T.LANES) * PIXEL_BYTES
        out.append(
            ISA.fill(addr=a + base + T.tap_offset(dy, dx, wp), n=conv.gm, fbank=bank)
        )
        out.append(
            ISA.fill(
                addr=b + (j * conv.steps + s) * conv.gn * T.FP16_ENTRY_BYTES,
                n=conv.gn,
                sel=1,
                fbank=bank,
            )
        )
        out.append(
            ISA.gemm(
                gm=conv.gm,
                gn=conv.gn,
                nk=1,
                acc=int(s > 0),
                abank=bank,
                bbank=bank,
            )
        )
    span = conv.gm * conv.gn
    out.append(
        ISA.drain(addr=c + (at * conv.tiles_n + j) * span * LO.WORD_BYTES, n=span)
    )
    return out


def run(conv: Conv, x, kernel, tiles=None, size: int = 1 << 25):
    """Execute `conv` on a SimDevice. Returns ``(HWC result, device, flits)``.

    `tiles` selects which M tiles to sweep; all of them by default. Positions no
    tile covers come back as zeros, which the caller compares nothing against.
    """
    tiles = list(range(conv.tiles_m)) if tiles is None else list(tiles)
    dev = SimDevice(base=MODEL_BASE, size=size)

    a_words = T.to_fp16_words_conv(x, conv.pad, conv.tail)
    lanes = conv.tiles_n * conv.gn * T.LANES
    b_pad = np.zeros((lanes, len(TAPS) * conv.cin), np.float16)
    b_pad[: conv.cout] = weights_for_k(np.asarray(kernel, np.float16), conv.cin)
    b_words = T.to_fp16_words_tiled(b_pad, conv.gn, 1)

    a = _upload(dev, a_words)
    b = _upload(dev, b_words)
    held = LO.Tile((len(tiles), conv.tiles_n), conv.gm, conv.gn)
    shape = (conv.rows(tiles), lanes)
    c = dev.alloc(held.nbytes(shape))

    payloads = {
        (n, j): flits(conv, a, b, c, tile, n, j)
        for n, tile in enumerate(tiles)
        for j in range(conv.tiles_n)
    }
    dev.dispatch(payloads, "MG", "conv2d")

    flat = held.unpack(dev.read(c, held.nbytes(shape)), shape)
    return _to_hwc(conv, flat, tiles), dev, sum(len(f) for f in payloads.values())


def _upload(dev, words) -> int:
    blob = b"".join(int(w).to_bytes(LO.WORD_BYTES, "little") for w in words)
    at = dev.alloc(len(blob))
    dev.write(at, blob)
    return at


def _to_hwc(conv: Conv, flat, tiles):
    """A `[M][N]` drain back to `[H][W][Cout]`, by the raster the sweep used."""
    _, wp, _ = conv.geometry
    out = np.zeros((conv.h, conv.w, conv.cout))
    per = conv.gm * T.LANES
    for n, tile in enumerate(tiles):
        for r in range(per):
            y, x = divmod(tile * per + r, wp)
            if y < conv.h and x < conv.w:
                out[y, x] = flat[n * per + r, : conv.cout]
    return out


def errors(got, want, tiles_seen) -> tuple:
    """``(p50 relative, max relative, max normalised)`` over the rows swept.

    The elementwise ratio is taken where the reference is above a thousandth of
    its own peak; below that it reports the reference's noise, not the machine's.
    """
    got, want = got[tiles_seen], want[tiles_seen]
    scale = np.abs(want).max()
    keep = np.abs(want) > scale / 1000
    rel = np.abs(got[keep] - want[keep]) / np.abs(want[keep])
    return (
        float(np.median(rel)),
        float(rel.max()),
        float(np.abs(got - want).max() / scale),
    )


def covered(conv: Conv, tiles):
    """A boolean `[H][W]` mask of the positions these M tiles produced."""
    _, wp, _ = conv.geometry
    mask = np.zeros((conv.h, conv.w), bool)
    per = conv.gm * T.LANES
    for tile in tiles:
        for r in range(per):
            y, x = divmod(tile * per + r, wp)
            if y < conv.h and x < conv.w:
                mask[y, x] = True
    return mask


# ------------------------------------------------------------------ the packer
@pytest.mark.parametrize(
    ("h", "w", "c", "pad"),
    [(8, 8, 32, 1), (5, 7, 64, 1), (13, 11, 96, 1), (4, 4, 32, 0)],
)
def test_the_packer_round_trips_bit_exactly(h, w, c, pad):
    rng = np.random.default_rng(0)
    x = rng.standard_normal((h, w, c)).astype(np.float16)
    words = T.to_fp16_words_conv(x, pad, tail=3)
    back = T.from_fp16_words_conv(words, x.shape, pad)
    assert back.dtype == np.float16
    assert np.array_equal(back.view(np.uint16), x.view(np.uint16))


def test_the_packed_bytes_are_the_layout_the_taps_assume():
    """A pixel's block is 64 B and its neighbour's is the next 64 B.

    The tap arithmetic is nothing but this, so it is asserted on the bytes
    rather than inferred from a matmul coming out right.
    """
    rng = np.random.default_rng(1)
    x = rng.standard_normal((6, 5, 64)).astype(np.float16)
    words = T.to_fp16_words_conv(x, pad=1)
    raw = b"".join(int(w).to_bytes(LO.WORD_BYTES, "little") for w in words)
    held = np.frombuffer(raw, np.float16)
    hp, wp, plane = T.conv_geometry(6, 5, 1)
    assert (hp, wp) == (8, 7)
    assert plane == 56 and plane % T.LANES == 0

    for cb in range(2):
        for y in range(6):
            for x0 in range(5):
                at = (cb * plane + (y + 1) * wp + (x0 + 1)) * T.KBLOCK
                block = held[at : at + T.KBLOCK]
                assert np.array_equal(block, x[y, x0, cb * 32 : cb * 32 + 32])
    assert not held[: T.KBLOCK].any(), "the halo is zero"


def test_a_tap_is_a_constant_byte_offset():
    """`tap_offset` names the same pixel the reference convolution reads."""
    _, wp, _ = T.conv_geometry(8, 8, 1)
    assert T.tap_offset(0, 0, wp) == 0
    assert T.tap_offset(0, 1, wp) == PIXEL_BYTES
    assert T.tap_offset(1, 0, wp) == wp * PIXEL_BYTES
    assert T.tap_offset(2, 2, wp) == (2 * wp + 2) * PIXEL_BYTES


# ------------------------------------------------------------------ arithmetic
@pytest.mark.parametrize(
    ("h", "w", "cin", "cout", "gm", "gn"),
    [
        (8, 8, 32, 32, 4, 8),
        (8, 8, 64, 64, 8, 16),
        (12, 10, 32, 96, 6, 24),
        (16, 16, 96, 64, 16, 16),
    ],
)
def test_conv2d_matches_a_direct_convolution(h, w, cin, cout, gm, gn):
    """The whole layer, every position, against float64 and against MXFP7.

    An addressing fault -- a tap on the wrong pixel, a channel block at the
    wrong stride, an entry straddling two blocks -- is an error of order one,
    which is why the tight bound is the one against `quantised`.
    """
    rng = np.random.default_rng(3)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    k = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    conv = Conv(h, w, cin, cout, gm=gm, gn=gn)
    tiles = range(conv.tiles_m)
    got, dev, _ = run(conv, x, k)
    mask = covered(conv, tiles)
    assert errors(got, quantised(conv, x, k, tiles), mask)[2] < 1e-3
    assert errors(got, reference(x, k), mask)[2] < 0.05
    assert dev.saturated == 0


@pytest.mark.parametrize("axis", [0, 1])
def test_the_test_would_notice_a_tap_off_by_one(axis):
    """The negative control: a one-pixel shift must fail both checks."""
    rng = np.random.default_rng(4)
    x = (rng.standard_normal((8, 8, 32)) * 0.5).astype(np.float16)
    k = (rng.standard_normal((32, 32, 3, 3)) * 0.2).astype(np.float16)
    conv = Conv(8, 8, 32, 32, gm=4, gn=8)
    got, _, _ = run(conv, x, k)
    shifted = reference(np.roll(x, 1, axis=axis), k)
    assert errors(got, shifted, covered(conv, range(conv.tiles_m)))[2] > 0.1


@pytest.mark.parametrize(
    ("h", "w", "c"), [(128, 128, 320), (64, 64, 640), (32, 32, 1280)]
)
def test_the_sdxl_shapes_are_right_at_their_own_geometry(h, w, c):
    """The three UNet convolutions, at four M tiles each.

    Whole layers are 41.6k, 10.4k and 2.6k passes; nothing about the addressing
    changes with the tile count, and what does change -- `wp`, the plane, the
    channel-block stride, the tap offsets -- is at its real value here.
    """
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((h, w, c)) * 0.5).astype(np.float16)
    k = (rng.standard_normal((16, c, 3, 3)) * 0.05).astype(np.float16)
    conv = Conv(h, w, c, 16, gm=8, gn=4)
    tiles = (0, 1, conv.tiles_m // 2, conv.tiles_m - 1)
    got, dev, sent = run(conv, x, k, tiles)
    mask = covered(conv, tiles)
    assert errors(got, quantised(conv, x, k, tiles), mask)[2] < 1e-3
    assert errors(got, reference(x, k), mask)[2] < 0.05
    assert dev.saturated == 0
    assert sent == len(tiles) * conv.tiles_n * conv.flits_per_pass


def test_nk_is_forced_to_one_by_the_layout():
    """Two channel blocks of one pixel are `plane*64` bytes apart, not adjacent.

    A fill of `nk` blocks is one contiguous run of `nk` entries, so nk > 1 would
    have to find block cb+1 at +256 B. It is at +plane*64. This is why the pass
    costs 3 flits per K block instead of amortising over nk.
    """
    conv = Conv(8, 8, 64, 32)
    _, _, plane = conv.geometry
    assert plane * PIXEL_BYTES != T.FP16_ENTRY_BYTES
    assert conv.steps == 9 * 2
    assert conv.flits_per_pass == 3 * 18 + 1


def test_the_dsl_kernel_emits_the_same_program_this_harness_does():
    """`kernels.conv2d` against the hand-computed schedule, to fp16 exactness.

    Two independent routes to the same fill addresses -- one written out here,
    one from `Slice`'s lane offset. Agreeing to 1e-6 says the DSL emits the
    program branch C asked for, not merely a program that convolves.
    """
    # The PLAIN conv is an op; `kernels.conv2d` is the bias/activation form.
    from kohakutpu import ops as KL

    h, w, cin, cout, gm, gn = 12, 10, 64, 64, 6, 16
    rng = np.random.default_rng(11)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    k = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)

    conv = Conv(h, w, cin, cout, gm=gm, gn=gn)
    harness, _, sent = run(conv, x, k)

    dev = SimDevice(size=1 << 25)
    held = KL.conv2d(
        dev.tensor(x), dev.tensor(KL.weights_for_k(k, cin)), gm=gm, gn=gn
    ).numpy()
    got = np.zeros((h, w, cout))
    for row, y, at in KL.positions(h, w):
        got[y, at] = held[row, :cout]

    mask = covered(conv, range(conv.tiles_m))
    assert np.abs(got[mask] - harness[mask]).max() < 1e-6
    assert dev.stats()["flits"] == sent


def test_a_pass_outruns_the_staging_window_and_still_accumulates():
    """433 flits for one tile against a 128-flit window, and one accumulation.

    `dispatch.plan` cuts an instance into windows and kicks them in order on one
    node, and nothing between them touches the accumulator -- so a sweep that
    spans four rounds still chains. That is what makes `nk = 1` affordable.
    """
    rng = np.random.default_rng(7)
    x = (rng.standard_normal((8, 8, 512)) * 0.5).astype(np.float16)
    k = (rng.standard_normal((16, 512, 3, 3)) * 0.05).astype(np.float16)
    conv = Conv(8, 8, 512, 16, gm=4, gn=4)
    assert conv.flits_per_pass == 433
    tiles = range(conv.tiles_m)
    got, dev, _ = run(conv, x, k, tiles)
    assert dev.stats()["rounds"] > conv.passes
    assert errors(got, quantised(conv, x, k, tiles), covered(conv, tiles))[2] < 1e-3


def test_a_fill_with_no_offset_is_quantised_to_whole_spans():
    """Why the offset had to exist: without one, no `gm`, `gn` or `nk` reaches a tap.

    Tile and chunk are multiplied by the fill's OWN span, so an offset-free fill
    lands on a multiple of `gm*nk*256` and the fills exactly tile the operand.
    """
    from kohakuaccel.lang import iface
    from kohakuaccel.machinespec import MachineSpec
    from kohakutpu.lang import BACKEND
    from kohakutpu.ops import matmul as mm

    machine = MachineSpec(
        name="gate", units={"MG": ((1, 1),), "VC": ((1, 0),)}, inst_depth=512
    )

    class Shaped:
        def __init__(self, shape):
            self.shape = shape

    for gm, nk in ((2, 2), (8, 1), (4, 4)):
        bound = {"a": Shaped((256, 512)), "b": Shaped((64, 512))}
        got = mm.compile(machine, iface.solve(mm.signature, bound), gm=gm, gn=1, nk=nk)
        span = gm * nk * T.FP16_ENTRY_BYTES
        for words in BACKEND.encode(
            got, got.stages[0], {"a": 0, "b": 0, "c": 0}
        ).values():
            for word in words:
                name, f = ISA.set.decode(word)
                if name == "FILL" and f["sel"] == 0:
                    assert f["addr"] % span == 0
        assert T.tap_offset(0, 1, 130) % span != 0


# ---------------------------------------------------------------- the caveats
def test_the_tail_padding_is_one_entry_at_the_sdxl_shapes():
    """§4 branch C's second caveat, derived rather than asserted."""
    for h, w, cin in ((128, 128, 320), (64, 64, 640), (32, 32, 1280)):
        conv = Conv(h, w, cin, cin, gm=8, gn=8)
        assert conv.tail == 1, (h, conv.tail)


def test_a_tapped_fill_straddles_a_4kb_boundary_once_every_16_entries():
    """§4 branch C's first caveat, counted on the addresses this emits.

    `mag_mem_port.v` has no split logic, so on SILICON these bursts are the open
    risk. The model reads memory flat and cannot see it, which is exactly why it
    is counted here instead of waited for.
    """
    conv = Conv(128, 128, 320, 320)
    _, wp, _ = conv.geometry
    per_tap = {}
    for dy, dx in TAPS:
        base = T.tap_offset(dy, dx, wp)
        starts = [base + e * T.FP16_ENTRY_BYTES for e in range(64)]
        over = sum(1 for s in starts if s % AXI_PAGE + T.FP16_ENTRY_BYTES > AXI_PAGE)
        per_tap[(dy, dx)] = over
    assert per_tap[(0, 0)] == 0, "the untapped fill is entry-aligned and safe"
    assert sum(per_tap.values()) == 4 * 6, "6 of the 9 taps, 4 of every 64 entries"
    assert all(v in (0, 4) for v in per_tap.values())
