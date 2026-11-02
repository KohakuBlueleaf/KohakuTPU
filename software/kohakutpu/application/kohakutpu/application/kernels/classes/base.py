"""Operands, plans and the rows store/block code every kernel class shares."""

import hashlib
from dataclasses import dataclass

from kohakutpu.application.kernels import expr as X
from kohakutpu.language import dsl as kt

L1_WORDS = 512

#: L1 words the lowering keeps free beside the operands (landing, slack).
L1_MARGIN = 16


@dataclass(frozen=True)
class Operand:
    """How a kernel reads one buffer, in elements of that buffer."""

    kind: str  # "full" | "tile" | "col" | "row" | "scalar"
    dtype: str = "f16"
    offset: int = 0  # element of row 0, column 0 (row, scalar: the value)
    pitch: int = 0  # elements between rows (full)
    rows: int = 0  # rows the buffer holds past the offset (its declared extent)


#: Rows a word of a statistic holds, and a trip's rows when one is read or
#: written.
STAT_ROWS = 16


def holds_stat(operands, out_kind: str) -> bool:
    return out_kind == "row" or any(o.kind == "row" for o in operands)


def stat_words(R: int) -> int:
    """Whole words of R statistic values."""
    return -(-R // STAT_ROWS)


@dataclass(frozen=True)
class Plan:
    """How a rows stage covers [R, W]: column blocks of `wc` (`nblk` of
    them), trips of `rows` rows (`nb` trips, `rp` rows with the tail)."""

    wc: int
    nblk: int
    rows: int
    nb: int
    rp: int


def name(prefix: str, *parts) -> str:
    h = hashlib.sha1(repr(parts).encode()).hexdigest()[:10]
    return f"{prefix}_{h}"


def port(o: Operand, rp: int, w: int):
    """The port and spec of an operand: a [rows, pitch] buffer covering its
    view (rows past R are the allocation's tail), a [W] row, an [rows]
    statistic, or a scalar's word."""
    if o.kind in ("full", "tile"):
        r0 = o.offset // o.pitch
        shape = (max(o.rows, r0 + rp), o.pitch)
    elif o.kind == "col":
        shape = (o.offset + w,)
    elif o.kind == "row":
        shape = (max(o.rows, stat_words(o.offset + rp) * STAT_ROWS),)
    else:
        shape = (max(o.rows, o.offset // STAT_ROWS * STAT_ROWS + STAT_ROWS),)
    return kt.In(*shape), kt.TensorSpec.make(shape, o.dtype)


def _rows_block(bufs, e, operands, out, pl: Plan, r, cb, pro: dict) -> None:
    """One band of rows from row `r`, column block `cb`."""
    col0 = cb * pl.wc
    vals = dict(pro)
    for i, o in enumerate(operands):
        buf = bufs[f"in{i}"]
        if o.kind in ("full", "tile"):
            rr, cc = divmod(o.offset, o.pitch)
            view = buf[rr + r : rr + r + pl.rows, cc + col0 : cc + col0 + pl.wc]
            vals[i] = kt.load(view, gt4=True)
        elif o.kind == "row":
            rr = o.offset + r
            vals[i] = kt.load(buf[rr : rr + pl.rows])
        elif o.kind == "scalar":
            w0 = o.offset // STAT_ROWS * STAT_ROWS
            lane = o.offset % STAT_ROWS
            vals[i] = kt.unpack(kt.load(buf[w0 : w0 + STAT_ROWS]), lane=lane)

    v = X.emit(e, vals)
    dst = bufs["out"]
    if out.kind == "row":
        if pl.nblk > 1:
            kt.store(dst[cb, r : r + pl.rows], v)
        else:
            kt.store(dst[r : r + pl.rows], v)
    else:
        rr, cc = divmod(out.offset, out.pitch)
        _store_over(dst, rr + r, cc + col0, pl.rows, pl.wc, v)


def _store_over(dst, r0, c0, rows: int, cols: int, v) -> None:
    """`v` over the [rows, cols] window of `dst` at (r0, c0): packed once,
    then stored over each of its bands. Statistics first materialise as
    register rows; their memory form remains 16 values a word."""
    if isinstance(v, kt.Value) and v.space == "l1" and len(v.shape) == 1:
        v = kt.unpack(v)  # a statistic as loaded (16 a word): to registers
    if not (isinstance(v, kt.Value) and v.space == "l1"):
        if not isinstance(v, kt.Value):
            raise ValueError("a rows kernel stores a computed value, not a number")
        if v.stat:
            v = kt.broadcast(v, 32)
        v = kt.pack(v, dst.spec.dtype, gt4=v.mode == "gt4")
    h, w = (v.shape[0], v.shape[-1]) if len(v.shape) == 2 else (v.shape[0], cols)
    if rows % h or cols % w:
        raise ValueError(f"a [{h}, {w}] value does not tile a [{rows}, {cols}] window")
    for i in range(0, rows, h):
        for j in range(0, cols, w):
            kt.store(dst[r0 + i : r0 + i + h, c0 + j : c0 + j + w], v)
