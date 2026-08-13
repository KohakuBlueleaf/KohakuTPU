"""A fill may start at a lane offset, and nothing that does not ask for one moves.

`Slice` carries an optional offset in LANES of the entry stream -- 64 bytes, a
quarter of an L1 entry -- which `TpuBackend._cluster` adds to the fill address.
It exists because a 3x3 convolution's nine taps are the same operand read at
nine constant offsets, six of which are not entry-aligned (`.plan/CONV2D.md`
§4, branch C), and no `gm`, `gn` or `nk` can place one.

The first two tests are the witness the rest of the change is measured against:
an offset nobody asks for must leave every shipped kernel byte-identical.
"""

import hashlib

import numpy as np
import pytest
from kohakuaccel.lang import iface
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.hw import tensor as T
from kohakutpu.isa import ISA
from kohakutpu.lang import BACKEND
from kohakutpu.lang.errors import LangError
from kohakutpu.model import SimDevice

from kohakutpu import kernels as K
from kohakutpu import ops as O

MACHINE = MachineSpec(
    name="witness",
    units={
        "MG": ((1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3)),
        "VC": ((2, 0), (2, 4)),
    },
    inst_depth=512,
    agent=(1, 0),
)

#: SHA-256 over every flit of every kernel below; the byte-identity claim.
#: Re-baselined from `3636a354...1863948d` when `ops` took the plain kernels.
BASELINE = "abe68e991c1a5e9d9b11b008a304187688e6401daa00623fdc1efcf04c094a5a"


class Shaped:
    def __init__(self, shape) -> None:
        self.shape = shape


#: Kernels and shapes covering both units and every fill this backend emits.
LIBRARY = [
    ("matmul", O.matmul, {"a": Shaped((64, 128)), "b": Shaped((32, 128))}, {}),
    (
        "batched_matmul",
        O.matmul,
        {"a": Shaped((4, 32, 64)), "b": Shaped((32, 64))},
        {},
    ),
    ("linear_silu", K.linear_silu, {"x": Shaped((32, 64)), "w": Shaped((64, 64))}, {}),
    ("rmsnorm", K.rmsnorm, {"x": Shaped((64, 64)), "w": Shaped((64, 64))}, {}),
    ("softmax", K.softmax, {"x": Shaped((64, 64))}, {}),
    ("silu", O.silu, {"x": Shaped((4096,))}, {}),
    ("residual", O.residual, {"a": Shaped((64, 64)), "b": Shaped((64, 64))}, {}),
    (
        "flash_attention",
        K.flash_attention,
        {"q": Shaped((64, 64)), "k": Shaped((64, 64)), "v": Shaped((64, 64))},
        {"block": 64},
    ),
]


def encoded(fn, bound, knobs):
    """Every flit `fn` compiles to at these shapes, in a settled order."""
    got = fn.compile(MACHINE, iface.solve(fn.signature, bound), **knobs)
    names = [p.name for p in fn.signature.ports] + list(got.temps)
    names += list(BACKEND.constants(got))
    addrs = {n: 0x1000 * (i + 1) for i, n in enumerate(names)}
    out = []
    for stage in got.stages:
        for at, words in sorted(BACKEND.encode(got, stage, addrs).items()):
            out.append((stage.index, at, tuple(words)))
    return got, out


def test_the_shipped_kernels_are_byte_identical():
    """The witness. Every flit of every kernel, against a digest predating this.

    A failure means a kernel that asked for no offset moved, which is the one
    thing the offset promised not to do. It also fires when a kernel's own
    default tiling is retuned, which is legitimate -- re-baseline deliberately,
    after checking `test_a_fill_with_no_offset_keeps_the_old_address` is green,
    since that one holds across a retune and is the durable claim.
    """
    h = hashlib.sha256()
    for name, fn, bound, knobs in LIBRARY:
        h.update(name.encode())
        for index, at, words in encoded(fn, bound, knobs)[1]:
            # Hex, not bytes: a vector flit carries a routing header above the
            # 256-bit payload and does not fit a fixed width.
            h.update(f"{index}{at}{[f'{w:x}' for w in words]}".encode())
    assert h.hexdigest() == BASELINE


@pytest.mark.parametrize("name", [row[0] for row in LIBRARY])
def test_a_fill_with_no_offset_keeps_the_old_address(name):
    """The formula this replaced, written out and checked against the encoder.

    Robust where the digest is brittle: this keeps holding when a kernel changes
    shape, and it is the property that actually has to be true.
    """
    _, fn, bound, knobs = next(row for row in LIBRARY if row[0] == name)
    got, program = encoded(fn, bound, knobs)
    addrs = {}
    names = [p.name for p in fn.signature.ports] + list(got.temps)
    names += list(BACKEND.constants(got))
    for i, n in enumerate(names):
        addrs[n] = 0x1000 * (i + 1)

    seen = 0
    for stage in got.stages:
        if stage.unit != "MG":
            continue
        for inst in stage.instances:
            for s in inst.stmts:
                if s.kind != "fill":
                    continue
                span = s.args["groups"] * s.args["blocks"]
                at = (s.args["tile"] * s.args["chunks"] + s.args["chunk"]) * span
                want = addrs[s.args["operand"]] + at * T.FP16_ENTRY_BYTES
                word = ISA.fill(
                    addr=want,
                    n=span,
                    sel=s.args["sel"],
                    fbank=int(s.args.get("step", s.args["chunk"])) % 2,
                )
                assert word in {w for _, _, ws in program for w in ws}
                seen += 1
    if any(stage.unit == "MG" for stage in got.stages):
        assert seen, f"{name} encoded no fill to check"


# ------------------------------------------------------------- the offset used
def conv_reference(x, kernel):
    """Direct convolution in float64, pad 1."""
    h, w, _ = x.shape
    xp = np.pad(np.asarray(x, np.float64), ((1, 1), (1, 1), (0, 0)))
    k = np.asarray(kernel, np.float64)
    out = np.zeros((h, w, k.shape[0]))
    for t in range(9):
        dy, dx = divmod(t, 3)
        out += np.tensordot(xp[dy : dy + h, dx : dx + w, :], k[:, :, dy, dx], (2, 1))
    return out


def run_conv(h, w, cin, cout, gm, gn, seed=11):
    """`conv2d` on a SimDevice. Returns ``(got, float64 reference, device)``."""
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    k = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    dev = SimDevice(size=1 << 25)
    flat = O.conv2d(dev.tensor(x), dev.tensor(O.weights_for_k(k, cin)), gm=gm, gn=gn)
    held = flat.numpy()
    got = np.zeros((h, w, cout))
    for row, y, at in O.positions(h, w):
        got[y, at] = held[row, :cout]
    return got, conv_reference(x, k), dev


@pytest.mark.parametrize(
    ("h", "w", "cin", "cout", "gm", "gn"),
    [(8, 8, 32, 32, 4, 8), (12, 10, 64, 64, 6, 16), (16, 16, 96, 32, 8, 8)],
)
def test_the_conv2d_kernel_computes_a_convolution(h, w, cin, cout, gm, gn):
    """The kernel, through the ordinary call path, against float64.

    1.6e-2 is this machine's MXFP7 floor on a contraction this long, not
    anything conv adds: `test_conv2d_branch_c` pins the same shapes to 3e-4
    against an MXFP7 reference.
    """
    got, want, dev = run_conv(h, w, cin, cout, gm, gn)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05
    assert dev.saturated == 0


def test_the_shipped_defaults_run_and_sit_near_the_tile_bound():
    """The default tiling is the whole cost of this kernel, so it is asserted.

    `gm=2, gn=1` costs 45 M flits on a 128x128x320 layer against 22 k near the
    bound. A default that far off is a defect, not a preference, so the shipped
    one is checked for both: it computes, and it is within a factor of two of
    `TILES = 512`.
    """
    got, want, dev = run_conv(
        12, 10, 64, 128, O.conv2d.signature.knobs["gm"], O.conv2d.signature.knobs["gn"]
    )
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05
    assert dev.saturated == 0

    gm, gn = O.conv2d.signature.knobs["gm"], O.conv2d.signature.knobs["gn"]
    assert 256 <= gm * gn <= 512, f"the default tile is {gm}x{gn} sub-tiles"
    assert gm <= 255 and gn <= 255, "a FILL names at most 255 entries"


def test_a_tile_at_the_uram_bound_still_computes():
    """`gm*gn = 4096` is what a URAM-tiled top allows, and it is 6x cheaper.

    Worth pinning because nothing else exercises a tile that large, and the tile
    address wraps SILENTLY past `TILES`.
    """
    got, want, dev = run_conv(12, 10, 32, 64, 64, 64)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05
    assert dev.saturated == 0


def test_the_plane_layout_round_trips_through_the_runtime():
    """`ConvEntry` is what the runtime uploads, so it has to invert as well."""
    from kohakutpu import layout as LO

    rng = np.random.default_rng(12)
    x = rng.standard_normal((9, 7, 64)).astype(np.float16)
    conv = LO.ConvEntry(1, 8)
    raw = conv.pack(x)
    assert len(raw) == conv.nbytes(x.shape)
    assert np.array_equal(conv.unpack(raw, x.shape), np.asarray(x, np.float64))


def test_the_tail_is_sized_for_the_tile_the_sweep_will_use():
    """`ConvEntry` takes `gm` because the tail is a property of the TILING.

    A taller tile overruns the plane by more, so a layout packed for gm=8 is
    short for gm=64 -- which is why the kernel hands its own `gm` to the layout
    rather than the layout assuming one.
    """
    from kohakutpu import layout as LO

    shape = (8, 8, 32)
    small = LO.ConvEntry(1, 8)
    large = LO.ConvEntry(1, 64)
    assert small.geometry(shape)[2] < large.geometry(shape)[2]
    assert small.nbytes(shape) < large.nbytes(shape)
    assert small.key != large.key


def test_a_fill_that_reaches_past_the_operand_is_refused():
    """The bound check counts the offset, in bytes, or a tap reads the arena.

    Off by a quarter entry is exactly the size the old entry-based check could
    not see, so the pair either side of the boundary is the point.
    """
    from kohakutpu.lang.backend import LANE_BYTES, _within

    bound = {"a": Shaped((64, 128)), "b": Shaped((32, 128))}
    got = O.matmul.compile(MACHINE, iface.solve(O.matmul.signature, bound))
    held = got.layouts["a"].nbytes(got.block("a"))
    span = held // T.FP16_ENTRY_BYTES

    _within(got, "a", 0, span, 0)
    with pytest.raises(LangError, match="reaches byte"):
        _within(got, "a", 0, span, LANE_BYTES)
