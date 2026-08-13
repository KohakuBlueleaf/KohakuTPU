"""Choosing the tiling a matched kernel is dispatched with.

tinygrad has nowhere to put a `gm` or a `gn`. Its scheduler decides where the
kernel boundaries are and this backend decides everything below them, so a
shape whose grid does not fit the vector cores would fail with advice --
"raise gm/gn until the grid fits" -- that nobody at the tensor level can act on.
The knobs are therefore read off the shape and the machine, here.

The kernel's OWN defaults are tried first and kept whenever they fit, so a shape
that ran before this existed dispatches the same program it always did.
"""

from kohakutpu.hw.veckernels import CHUNK_WORDS, require_l1
from kohakutpu.isa.vecemit import AGU_WALK

from kohakutpu import layout as LO

LANES = LO.LANES

#: A FILL names at most 255 entries, so neither side of a tile may exceed it.
FILL_ENTRIES = 255

#: Kernels whose epilogue rides the resident tile, so their GRID must fit the
#: vector cores. Where it cannot, `Kernel.relax` stages it and this only tiles.
FUSED = ("linear_silu", "linear_relu", "linear_gelu", "linear_scale")


def groups(n: int) -> int:
    """Lane groups in `n` rows, which is what a tile is counted in."""
    return -(-n // LANES)


def fused_tile(gm: int, gn: int) -> bool:
    """Whether a `gm x gn` tile can be handed to a vector core over the NoC.

    Asks `require_l1` rather than restating its bad band: the emitter refuses a
    footprint the card returns wrong data for, and a second copy of that rule
    here would drift from it silently.
    """
    words = gm * gn
    if words > AGU_WALK or gm > FILL_ENTRIES or gn > FILL_ENTRIES:
        return False
    try:
        require_l1("", 2 * (-(-words // CHUNK_WORDS) * CHUNK_WORDS))
    except ValueError:
        return False
    return True


def fused_grid(gm: int, gn: int, m: int, n: int, cores: int) -> bool:
    """Whether an `m x n` result tiled this way fits `cores` receivers.

    A vector core takes one open stream at a time, so the grid may hold no more
    instances than there are cores.
    """
    if not fused_tile(gm, gn):
        return False
    rows, cols = -(-groups(m) // gm), -(-groups(n) // gn)
    return rows * cols <= cores


def _candidates(m: int, n: int, cores: int):
    """Every `(gm, gn)` a fused grid of at most `cores` instances can use.

    Enumerated by GRID rather than by tile: an instance count fixes the tile
    that covers the shape with no waste, and every other tile of that grid is
    that one padded.
    """
    gy, gx = groups(m), groups(n)
    for rows in range(1, cores + 1):
        for cols in range(1, cores // rows + 1):
            gm, gn = -(-gy // rows), -(-gx // cols)
            if fused_grid(gm, gn, m, n, cores):
                yield gm, gn


def _score(gm: int, gn: int, m: int, n: int) -> tuple:
    """How good a tiling is: FEWEST instances, then arithmetic intensity.

    A fused instance carries a whole vector image, so splitting the same result
    over more of them lengthens the instruction stream and shortens nothing.
    Measured on `linear_relu` at 64x128x128: two instances is 364 flits and four
    is 408, at five rounds either way. Intensity is `2*gm*gn/(gm+gn)`, maximal
    on a square tile; `gm` breaks the remaining tie so the choice is fixed.
    """
    instances = -(-groups(m) // gm) * -(-groups(n) // gn)
    return -instances, 2 * gm * gn / (gm + gn), gm


def plan(kernel, m: int, n: int, cores: int, k: int = 0, machine=None) -> tuple:
    """`(kernel, knobs)` for one library kernel at an `m x n` result.

    Returns `kernel` itself with its own defaults whenever those fit. For a
    fused kernel whose grid they do not fit, returns the tiling `_score` ranks
    highest; where no tiling fits, the kernel goes back UNCHANGED and the
    compiler stages the epilogue itself.

    `k` and `machine` are accepted and unused. They priced a `*_separated`
    twin, and those kernels are gone: the compiler stages a fusion it cannot
    perform, so there is no second kernel to weigh. Restoring the choice needs
    `fuse` DECLARED as a knob -- it is set internally by `relax` today, and an
    undeclared knob is filtered out of a call silently. What that would buy is
    measured: `linear_relu` at 64x?x128 crosses over between K=768 and K=896
    while `linear_silu` at the same shape does not cross by K=1152.
    """
    knobs = dict(kernel.signature.knobs)
    if kernel.name not in FUSED:
        return kernel, knobs
    if fused_grid(knobs["gm"], knobs["gn"], m, n, cores):
        return kernel, knobs
    fits = list(_candidates(m, n, cores))
    if not fits:
        return kernel, knobs
    gm, gn = max(fits, key=lambda pair: _score(*pair, m, n))
    return kernel, {**knobs, "gm": gm, "gn": gn}


class _Shaped:
    """What `iface.solve` needs of an argument, without a device to put it on."""

    def __init__(self, shape) -> None:
        self.shape = shape


#: `(kernel, machine, m, k, n, knobs)` -> words. Encoding a 256x1024x4096 tile
#: is ~1 s, and a planner is asked the same question once per dispatch.
_PRICED: dict = {}


def _flits(kernel, machine, m: int, k: int, n: int, knobs: dict):
    """Instruction words `kernel` emits at these extents, or None if it refuses.

    Both operands are named for the `linear_*` family's own ports, which is the
    only family `SEPARATED` covers.
    """
    from kohakuaccel.lang import iface
    from kohakutpu.lang import BACKEND

    key = (kernel.name, id(machine), m, k, n, tuple(sorted(knobs.items())))
    if key in _PRICED:
        return _PRICED[key]
    bound = {"x": _Shaped((m, k)), "w": _Shaped((n, k))}
    try:
        got = kernel.compile(machine, iface.solve(kernel.signature, bound), **knobs)
        names = [p.name for p in kernel.signature.ports] + list(got.temps)
        names += list(BACKEND.constants(got))
        addrs = {name: 0x100000 * (i + 1) for i, name in enumerate(names)}
        words = sum(
            len(w)
            for stage in got.stages
            for w in BACKEND.encode(got, stage, addrs).values()
        )
    except Exception:  # noqa: BLE001
        words = None
    _PRICED[key] = words
    return words
