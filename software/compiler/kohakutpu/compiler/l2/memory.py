"""Views of memory buffers as byte runs, in each buffer layout
(`kohakutpu.compiler.layout.buffer`).

A view's axes, evaluated: ``(start, size, kept)`` each -- `I` is
``(I, 1, False)``, `LO : +N` ``(LO, N, True)``, `:` the whole axis.
"""

from kohakutpu.compiler.layout.tensor import KBLOCK, LANES
from kohakutpu.language.text.syntax import Int, NewAxis, Pos, Slice, View

ENTRY = 128
SUBTILE = 32


class ViewError(ValueError):
    pass


def axes(view: View, shape: tuple) -> list:
    """``[(start, size, kept)]`` of an evaluated view over `shape`; a new axis
    (`*`) is ``(0, 1, None)``. Raises `ViewError` outside the shape."""
    out, d = [], 0
    for s in view.slices:
        if isinstance(s, NewAxis):
            out.append((0, 1, None))
            continue
        if d >= len(shape):
            raise ViewError(f"{view.name} has {len(shape)} axes")
        n = shape[d]
        if isinstance(s, Int):
            lo, size, kept = s.value, 1, False
        elif isinstance(s, Slice):
            lo = 0 if s.lo is None else _int(s.lo, view)
            if s.hi is None:
                hi = n
            elif isinstance(s.hi, Pos):
                hi = lo + _int(s.hi.x, view)
            else:
                hi = _int(s.hi, view)
            size, kept = hi - lo, True
        else:
            raise ViewError(f"{view.name}: an axis is I, LO : +N, LO : HI or :")
        if lo < 0 or size <= 0 or lo + size > n:
            raise ViewError(f"{view.name} axis {d}: [{lo}, {lo + size}) of {n}")
        out.append((lo, size, kept))
        d += 1
    while d < len(shape):
        out.append((0, shape[d], True))
        d += 1
    return out


def _int(t, view) -> int:
    if not isinstance(t, Int):
        raise ViewError(f"{view.name}: a bound is a constant here, got {t!r}")
    return t.value


def shape_of(ax: list) -> tuple:
    """The view's shape: kept axes and new ones."""
    return tuple(size for _, size, kept in ax if kept is not False)


def _slab(buf, ax: list) -> tuple:
    """``(byte offset of the slab, [(start, size)] of its last two axes)``:
    the leading axes must pick one slab."""
    lead = buf.shape[:-2]
    real = [a for a in ax if a[2] is not None]
    off, stride = 0, buf.nbytes
    for (lo, size, _), n in zip(real[: len(lead)], lead, strict=True):
        if size != 1:
            raise ViewError("a view picks one slab of a batched buffer")
        stride //= n
        off += lo * stride
    return off, [(lo, size) for lo, size, _ in real[len(lead) :]]


# ------------------------------------------------------------- cluster fills
def fill_runs(buf, ax: list) -> list:
    """``[(byte offset, entries, first L1 entry)]`` a FILL of the view reads,
    for an `mx7a` / `mx7b` buffer packed tile-major (`tensor.pack`): the view's
    K must be one packed chunk, its rows whole lane groups; rows crossing a
    packed tile are two runs."""
    if buf.dtype not in ("mx7a", "mx7b") or buf.layout.get("t"):
        raise ViewError(f"a fill reads an mx7a / mx7b buffer, not {buf.dtype}")
    g = buf.layout["gm" if buf.dtype == "mx7a" else "gn"]
    nk = buf.layout["nk"]
    base, ((r0, rows), (k0, k)) = _slab(buf, ax)
    kt = buf.shape[-1]
    chunk = nk * KBLOCK
    if k != chunk or k0 % chunk:
        raise ViewError(
            f"a fill's K is one packed chunk ({chunk} at a multiple of {chunk}); "
            f"got [{k0}, {k0 + k})"
        )
    if r0 % LANES or rows % LANES:
        raise ViewError(f"a fill's rows are whole groups of {LANES}")
    chunks = kt // chunk
    c = k0 // chunk
    out, lo, hi = [], r0 // LANES, (r0 + rows) // LANES
    eoff = 0
    while lo < hi:
        t = lo // g
        top = min(hi, (t + 1) * g)
        first = ((t * chunks + c) * g + (lo - t * g)) * nk
        n = (top - lo) * nk
        out.append((base + first * ENTRY, n, eoff))
        eoff += n
        lo = top
    return out


# ------------------------------------------------------------------ drains
def subtile_address(buf, r: int, c: int) -> int:
    """Byte offset of sub-tile (r, c) (4x4 elements) of an `f16` buffer with a
    `tile` layout."""
    gm, gn = buf.layout["tile"]
    n = buf.shape[-1]
    tn = n // (4 * gn)
    ti, tj = r // gm, c // gn
    return ((ti * tn + tj) * gm * gn + (r % gm) * gn + c % gn) * SUBTILE


def drain_base(buf, ax: list) -> int:
    """Byte offset of a drain of the view: an `f16 @dram(tile=..)` region
    whose 4x4 sub-tiles, row-major over the view, are consecutive 32-byte
    words (the order a sweep emits them)."""
    if buf.dtype != "f16" or "tile" not in buf.layout:
        raise ViewError("a drain to memory writes an f16 buffer with a tile= layout")
    base, ((r0, rows), (c0, cols)) = _slab(buf, ax)
    if r0 % 4 or c0 % 4 or rows % 4 or cols % 4:
        raise ViewError("a drained view is whole 4x4 sub-tiles")
    sr, sc, nr, nc = r0 // 4, c0 // 4, rows // 4, cols // 4
    first = subtile_address(buf, sr, sc)
    for r in range(nr):
        for c in (0, nc - 1):
            want = first + (r * nc + c) * SUBTILE
            if subtile_address(buf, sr + r, sc + c) != want:
                raise ViewError(
                    f"rows [{r0}, {r0 + rows}) x cols [{c0}, {c0 + cols}) are not "
                    f"one run in tile={buf.layout['tile']}: a drain writes its "
                    f"sub-tiles row-major, one after another"
                )
    return base + first


# ------------------------------------------------------- row-major regions
def rowmajor_walk(buf, ax: list) -> tuple:
    """``(byte offset, [(count, stride)] outermost first, element bytes)`` of
    a view of a row-major buffer (no layout)."""
    if buf.layout:
        raise ViewError(f"{buf.dtype} with layout {buf.layout} is not row-major")
    item = buf.nbytes // _count(buf.shape)
    strides, s = [], item
    for n in reversed(buf.shape):
        strides.insert(0, s)
        s *= n
    off, dims = 0, []
    real = [a for a in ax if a[2] is not None]
    for (lo, size, _), st in zip(real, strides, strict=True):
        off += lo * st
        if size > 1:
            dims.append((size, st))
    return off, dims, item


def _count(shape) -> int:
    n = 1
    for d in shape:
        n *= d
    return n


__all__ = [
    "ENTRY",
    "SUBTILE",
    "ViewError",
    "axes",
    "drain_base",
    "fill_runs",
    "rowmajor_walk",
    "shape_of",
    "subtile_address",
]
