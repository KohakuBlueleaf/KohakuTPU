"""Memory-mover moves as register writes, for a package's MOVER step.

The compiler's copy of the mover's command encoding (control-registers.md s3,
transform-slot.md): what a node writes, register by register, to start a move.
`kohakuaccel.driver.device.mover` is the host's copy; the two are compared by
software/tests/test_package_mover.py, since nothing else ties them.

A walker is a :class:`Walk` (or ``(base, dims)``): `dims` is one entry per
level, OUTERMOST FIRST -- ``(count, stride)``, or ``(count, stride, axis,
astep)`` to step bound axis 1 or 2 by `astep` -- strides in bytes and
multiples of 32. `windows` bounds axes 1 and 2 as ``(base, extent)``; an
element outside a window reads the immediate (source) or is not written
(destination), and an extent of 0 disables the axis.
"""

from dataclasses import dataclass

from kohakuaccel.compiler.package.format import PackageError

R_CTRL, R_HDR, R_DIM, R_AXIS, R_WINDOW = 0x00, 0x10, 0x18, 0x20, 0x28
R_INDEX, R_SEED, R_IMM, R_ROW = 0x30, 0x38, 0x40, 0x50
COPY, GATHER, GENERATE, FILL, XFORM, SCATTER = 0, 2, 3, 4, 5, 6
MODES = {
    "copy": COPY,
    "gather": GATHER,
    "generate": GENERATE,
    "fill": FILL,
    "xform": XFORM,
    "scatter": SCATTER,
}
W8, W16, W32 = 0, 1, 2
#: flags[3]: coalesce writes into bursts.
FLAG_WCOAL = 1 << 3
#: flags[7:5]: a burst cap of 2^(n-1) beats, 0 the engine's own maximum.
BURST_LSB = 5
GO = 1 << 16
#: CTRL[17]: the index list holds fp32 values (a vector core's F32 pack).
INDEX_FP = 1 << 17
WORD_BYTES = 32
#: Words one move carries at most, well under a dimension's 16 bits: the node
#: bounds a MOVER step's wait by PROGRESS (a move finishing), so one move must
#: finish inside the node's timeout at the slowest transform's rate.
MOVE_WORDS = 1 << 14
ADDR_BITS = 40
NDIM = 6
#: Indices one gather or scatter takes (`IDX_WORDS` x 8, mm_mover.v).
MAX_INDEX = 2048
#: Bits of the header word: base at [43:4], transform id [50:47], mode [58:55].
BASE_LSB, XID_LSB, XMODE_LSB = 4, 47, 55


def _fits(name: str, value: int, bits: int, signed: bool = False) -> int:
    lo, hi = (-(1 << (bits - 1)), 1 << (bits - 1)) if signed else (0, 1 << bits)
    if not lo <= value < hi:
        raise PackageError(f"mover {name} {value:#x} does not fit {bits} bits")
    return value & ((1 << bits) - 1)


@dataclass(frozen=True)
class Walk:
    """One walker: a base, its levels outermost first, two bound windows."""

    base: int
    dims: tuple
    windows: tuple = ((0, 0), (0, 0))

    @classmethod
    def of(cls, w) -> "Walk":
        if isinstance(w, Walk):
            return w
        base, dims = w
        return cls(base, tuple(tuple(d) for d in dims))

    def words(self) -> int:
        n = 1
        for d in self.dims:
            n *= d[0]
        return n


def walker(sel: int, walk, xform_id: int = 0, xform_mode: int = 0) -> list:
    """The writes that load one walker: header, each dimension, both windows.

    Both window extents are written (0, disabled) because an omitted one
    INHERITS the previous move's and silently suppresses writes. A dimension
    is staged by 0x18 and latched by 0x20, so every level writes both.
    """
    w = Walk.of(walk)
    if not 1 <= len(w.dims) <= NDIM:
        raise PackageError(f"a walker has 1..{NDIM} dimensions, got {len(w.dims)}")
    if w.base % WORD_BYTES or any(d[1] % WORD_BYTES for d in w.dims):
        raise PackageError("mover addresses and strides are whole 32-byte words")
    out = [
        (
            R_HDR,
            sel
            | _fits("base", w.base, ADDR_BITS) << BASE_LSB
            | len(w.dims) << 44
            | _fits("transform id", xform_id, 4) << XID_LSB
            | _fits("transform mode", xform_mode, 4) << XMODE_LSB,
        )
    ]
    for at, d in enumerate(w.dims):
        count, stride = d[0], d[1]
        axis, astep = (d[2], d[3]) if len(d) == 4 else (0, 0)
        if axis not in (0, 1, 2):
            raise PackageError(f"a level binds axis 0 (none), 1 or 2, not {axis}")
        out.append(
            (
                R_DIM,
                sel
                | at << 1
                | _fits("count", count, 16) << 4
                | _fits("stride", stride, 32, signed=True) << 20,
            )
        )
        out.append((R_AXIS, axis | _fits("axis step", astep, 16, signed=True) << 2))
    if len(w.windows) != 2:
        raise PackageError("a walker has two windows, for axes 1 and 2")
    for axis, (abase, extent) in enumerate(w.windows):
        out.append(
            (
                R_WINDOW,
                sel
                | axis << 1
                | _fits("window base", abase, 16, signed=True) << 2
                | _fits("window extent", extent, 16) << 18,
            )
        )
    return out


def flag_byte(wcoal: bool = True, burst: int = 0) -> int:
    """The flags byte: write coalescing, and a burst cap of 2^(burst-1) beats
    (0: the engine's own maximum)."""
    return (FLAG_WCOAL if wcoal else 0) | _fits("burst cap", burst, 3) << BURST_LSB


def move(
    mode: int,
    src=None,
    dst=None,
    ewidth: int = W32,
    flags: int = FLAG_WCOAL,
    xform=(0, 0),
    imm: int | None = None,
    seed: int | None = None,
    index=None,
    row=None,
    fp: bool = False,
) -> list:
    """One move, GO last. `src` / `dst` are walkers (a FILL or GENERATE has no
    source); `imm` is the fill value and the padding, `seed` the generator's;
    `index` is ``(base, count)`` and `row` ``(pitch, words)`` for a gather or
    scatter. `flags` is the flags byte (:func:`flag_byte`)."""
    out = []
    if src is not None:
        out += walker(0, src, *xform)
    if dst is not None:
        out += walker(1, dst)
    if index is not None:
        base, count = index
        if base % WORD_BYTES or not 0 < count <= MAX_INDEX:
            raise PackageError(f"an index list of {count} at {base:#x}")
        out.append((R_INDEX, _fits("index base", base, ADDR_BITS) | count << 40))
    if row is not None:
        pitch, words = row
        out.append(
            (R_ROW, _fits("pitch", pitch, 32) | _fits("row words", words, 16) << 32)
        )
    if seed is not None:
        out.append((R_SEED, _fits("seed", seed, 64)))
    if imm is not None:
        out.append((R_IMM, _fits("immediate", imm, 32)))
    if ewidth not in (W8, W16, W32):
        raise PackageError(f"element width {ewidth} is not 8, 16 or 32 bits")
    ctl = _fits("mode", mode, 3) | ewidth << 3 | (flags & 0xFF) << 8 | GO
    out.append((R_CTRL, ctl | (INDEX_FP if fp else 0)))
    return out


def copy(src: int, dst: int, nbytes: int) -> list:
    """`nbytes` (whole words) from `src` to `dst`, contiguous. More than
    `MOVE_WORDS` is several moves."""
    words = nbytes // WORD_BYTES
    step = MOVE_WORDS
    out = []
    for at in range(0, words, step):
        n, off = min(step, words - at), at * WORD_BYTES
        out += move(
            COPY, (src + off, [(n, WORD_BYTES)]), (dst + off, [(n, WORD_BYTES)])
        )
    return out


def convert(
    src: int,
    dst: int,
    entries: int,
    xform_id: int = 1,
    mode: int = 0,
    in_words: int = 8,
    out_words: int = 4,
) -> list:
    """A converting move: `entries` source entries of `in_words` words each,
    through transform `xform_id`, each written as `out_words` words.

    The SOURCE walker counts source words and defines the iteration space; the
    destination steps once per entry (mm_mover.v MODE_XFORM). No bound axis:
    a transform move tiles whole entries. More than `MOVE_WORDS` source words
    is several moves, each of whole entries.
    """
    step = MOVE_WORDS // in_words
    out = []
    for at in range(0, entries, step):
        n = min(step, entries - at)
        out += move(
            XFORM,
            (src + at * in_words * WORD_BYTES, [(n * in_words, WORD_BYTES)]),
            (dst + at * out_words * WORD_BYTES, [(n, out_words * WORD_BYTES)]),
            ewidth=W16,
            xform=(xform_id, mode),
        )
    return out


def convert_walk(src, dst, xform_id: int = 1, mode: int = 0) -> list:
    """A converting move over explicit walkers: `src` counts SOURCE words,
    `dst` steps once per entry -- so an im2col gather and the quantise are one
    move."""
    return move(XFORM, src, dst, ewidth=W16, xform=(xform_id, mode))


def fill(dst, value: int = 0, ewidth: int = W32) -> list:
    """Write `value` over `dst`'s walk, reading nothing: a 32-bit immediate,
    or one of `ewidth` replicated. The immediate is written every time: an
    omitted one is the previous move's."""
    return move(FILL, None, dst, ewidth=ewidth, imm=value)


def generate(dst, seed: int, ewidth: int = W32) -> list:
    """Pseudo-random words over `dst`'s walk, keyed by `seed` and each word's
    absolute destination address (so any split of the walk writes the same)."""
    return move(GENERATE, None, dst, ewidth=ewidth, seed=seed)


def gather(src_base: int, index, dst, row, fp: bool = False) -> list:
    """Row r of `dst`'s walk := the `row[1]` words at src_base + index[r] *
    row[0]; `index` is ``(base, count)``, its entries u32 (fp32 with `fp`)."""
    return move(GATHER, (src_base, [(1, WORD_BYTES)]), dst, index=index, row=row, fp=fp)


def scatter(src, dst_base: int, index, row, fp: bool = False) -> list:
    """Row r of `src`'s walk -> the `row[1]` words at dst_base + index[r] *
    row[0]; the source walk defines the iteration space."""
    return move(
        SCATTER, src, (dst_base, [(1, WORD_BYTES)]), index=index, row=row, fp=fp
    )


def fields(writes: list) -> dict:
    """``{(register, name): (pair index, bit in the 64-bit value, width)}`` of
    the fields a register expression may set in `writes` (an L1 operand held
    in a package register): bases, counts, index count, pitch, words, seed,
    immediate. The LAST write of each register names it."""
    out = {}
    names = {
        R_HDR: (("base", BASE_LSB, ADDR_BITS),),
        R_INDEX: (("index", 0, ADDR_BITS), ("count", 40, 16)),
        R_ROW: (("pitch", 0, 32), ("words", 32, 16)),
        R_SEED: (("seed", 0, 64),),
        R_IMM: (("imm", 0, 32),),
    }
    for i, (reg, value) in enumerate(writes):
        for name, bit, width in names.get(reg, ()):
            key = (reg, name)
            if reg == R_HDR:
                key = (reg, f"{'dst' if value & 1 else 'src'}.{name}")
            out[key] = (i, bit, width)
        if reg == R_DIM:
            out[(reg, f"{'dst' if value & 1 else 'src'}.count{(value >> 1) & 7}")] = (
                i,
                4,
                16,
            )
    return out


def addresses(word: int, unit_type: str = "mover") -> list:
    """Address fields of one MOVER payload (two register/value pairs): every
    walker header's base and an index list's, as ``[(segments, value)]`` for
    relocation."""
    out = []
    for half in (0, 128):
        reg = (word >> half) & ((1 << 64) - 1)
        lsb = {R_HDR: BASE_LSB, R_INDEX: 0}.get(reg)
        if lsb is None:
            continue
        bit = half + 64 + lsb
        value = (word >> bit) & ((1 << ADDR_BITS) - 1)
        out.append((((bit, ADDR_BITS, 0),), value))
    return out
