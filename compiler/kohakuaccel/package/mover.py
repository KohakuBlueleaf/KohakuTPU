"""Memory-mover moves as register writes, for a package's MOVER step.

The compiler's copy of the mover's command encoding (control-registers.md s3,
transform-slot.md): what a node writes, register by register, to start a move.
`driver/kohakuaccel/device/mover.py` is the host's copy; the two are compared
by compiler/tests/test_package_mover.py, since nothing else ties them.

A walker is ``(base, dims)``, `dims` being ``(count, stride)`` per level,
OUTERMOST FIRST, strides in bytes and multiples of 32.
"""

from kohakuaccel.package.format import PackageError

R_CTRL, R_HDR, R_DIM, R_AXIS, R_WINDOW = 0x00, 0x10, 0x18, 0x20, 0x28
R_IMM = 0x40
COPY, FILL, XFORM = 0, 4, 5
W16, W32 = 1, 2
#: flags[3]: coalesce writes into bursts.
FLAG_WCOAL = 1 << 3
GO = 1 << 16
WORD_BYTES = 32
#: Words one move carries at most, well under a dimension's 16 bits: the node
#: bounds a MOVER step's wait by PROGRESS (a move finishing), and a 512 KB move
#: finishes in ~500k cycles even at the quantiser's ~1 B a cycle.
MOVE_WORDS = 1 << 14
ADDR_BITS = 40
NDIM = 6
#: Bits of the header word: base at [43:4], transform id [50:47], mode [58:55].
BASE_LSB, XID_LSB, XMODE_LSB = 4, 47, 55


def _fits(name: str, value: int, bits: int) -> int:
    if not 0 <= value < 1 << bits:
        raise PackageError(f"mover {name} {value:#x} does not fit {bits} bits")
    return value


def walker(sel: int, base: int, dims, xform_id: int = 0, xform_mode: int = 0) -> list:
    """The writes that load one walker: header, each dimension, both windows.

    Both window extents are written (0, disabled) because an omitted one
    INHERITS the previous move's and silently suppresses writes.
    """
    dims = list(dims)
    if not 1 <= len(dims) <= NDIM:
        raise PackageError(f"a walker has 1..{NDIM} dimensions, got {len(dims)}")
    if base % WORD_BYTES or any(s % WORD_BYTES for _, s in dims):
        raise PackageError("mover addresses and strides are whole 32-byte words")
    out = [
        (
            R_HDR,
            sel
            | _fits("base", base, ADDR_BITS) << BASE_LSB
            | len(dims) << 44
            | _fits("transform id", xform_id, 4) << XID_LSB
            | _fits("transform mode", xform_mode, 4) << XMODE_LSB,
        )
    ]
    for at, (count, stride) in enumerate(dims):
        out.append(
            (
                R_DIM,
                sel
                | at << 1
                | _fits("count", count, 16) << 4
                | (stride & 0xFFFF_FFFF) << 20,
            )
        )
        out.append((R_AXIS, 0))
    out += [(R_WINDOW, sel | axis << 1) for axis in (0, 1)]
    return out


def move(
    mode: int, src, dst, ewidth: int = W32, flags: int = FLAG_WCOAL, xform=(0, 0)
) -> list:
    """One move: `src` and `dst` are ``(base, dims)``; GO is the last write."""
    return [
        *walker(0, src[0], src[1], *xform),
        *walker(1, dst[0], dst[1]),
        (R_CTRL, mode | ewidth << 3 | (flags & 0xFF) << 8 | GO),
    ]


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
    """A converting move over explicit walkers, ``(base, dims)`` each: `src`
    counts SOURCE words, eight to an entry, `dst` steps once per entry -- so an
    im2col gather and the quantise are one move."""
    return move(XFORM, src, dst, ewidth=W16, xform=(xform_id, mode))


def fill(dst, value: int = 0) -> list:
    """Write the 32-bit `value` over `dst`'s walk, reading nothing.

    The immediate is written every time: an omitted one is the previous FILL's.
    """
    return [
        *walker(1, dst[0], dst[1]),
        (R_IMM, _fits("immediate", value, 32)),
        (R_CTRL, FILL | W32 << 3 | (FLAG_WCOAL & 0xFF) << 8 | GO),
    ]


def addresses(word: int, unit_type: str = "mover") -> list:
    """Address fields of one MOVER payload (two register/value pairs): every
    walker header's base, as ``[(segments, value)]`` for relocation."""
    out = []
    for half in (0, 128):
        reg = (word >> half) & ((1 << 64) - 1)
        if reg != R_HDR:
            continue
        bit = half + 64 + BASE_LSB
        value = (word >> bit) & ((1 << ADDR_BITS) - 1)
        out.append((((bit, ADDR_BITS, 0),), value))
    return out
