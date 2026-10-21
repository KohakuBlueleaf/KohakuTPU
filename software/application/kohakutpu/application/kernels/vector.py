"""Generated vector-core kernels: an expression over operands, as a DSL kernel.

`rows_kernel`: [R, W] f16 rows (W a multiple of 32) in GT4 bands of 4. An
operand is `full` (an [R, W] view, rows a pitch apart), `tile` (the same of
a cluster's drained sub-tiles), `col` (one [W] row every row reads), `row` (a
statistic held as [R, 32], its value in all 32 columns) or `scalar` (row 0
of one). The output is an [R, W] view, or with a `Reduce` a [R, 32]
statistic; rows wider than a band's registers go in column blocks, a
reduction across them leaving partials to fold again. `flat_kernel`: N
elements as 128-value rows (f16 or f32), each operand read at the output's
index. A core's trips are a constant of the instance (``h + c * (n - 2h)``
on two cores).
"""

import functools
import hashlib
import operator
from dataclasses import dataclass

from kohakutpu.application.kernels import expr as X
from kohakutpu.language import dsl as kt

L1_WORDS = 512
#: L1 words the lowering keeps free beside the operands (landing, slack).
L1_MARGIN = 16
#: Widest column block one band's registers take: 8 registers a value.
MAX_BLOCK = 256


@dataclass(frozen=True)
class Operand:
    """How a kernel reads one buffer, in elements of that buffer."""

    kind: str  # "full" | "tile" | "col" | "row" | "scalar"
    dtype: str = "f16"
    offset: int = 0  # element of row 0, column 0
    pitch: int = 0  # elements between rows (full)
    rows: int = 0  # rows the buffer holds past the offset (its declared extent)


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


def split(n: int, cores: int):
    """``(cores, start(c), trips(c))`` of `n` trips over `cores` cores."""
    cores = max(1, min(cores, n))
    if cores == 1:
        return 1, (lambda c: 0), (lambda c: n)
    h = -(-n // 2)
    return 2, (lambda c: c * h), (lambda c: h + c * (n - 2 * h))


def block(w: int, most: int = MAX_BLOCK) -> int:
    for wc in (256, 128, 64, 32):
        if wc <= most and w % wc == 0:
            return wc
    raise ValueError(f"{w} columns do not split into blocks of 32")


def _bands_fit(operands, out_kind: str, wc: int, W: int) -> int:
    """Bands of 4 rows x `wc` columns the L1 holds double-buffered, beside
    the prologue (a phase is one column block of W)."""
    # A band of 4 rows x wc f16 columns is wc / 4 L1 words; a statistic's 8.
    words = sum(wc // 4 for o in operands if o.kind in ("full", "tile"))
    words += sum(8 for o in operands if o.kind == "row")
    words += 8 if out_kind == "row" else wc // 4
    prologue = sum(wc // 16 for o in operands if o.kind == "col")
    prologue += sum(2 for o in operands if o.kind == "scalar")
    room = L1_WORDS - L1_MARGIN - prologue
    return room // (2 * words) if words else 4


def plan(
    operands,
    out_kind: str,
    R: int,
    W: int,
    bands=None,
    wc=None,
    quantum: int = 4,
) -> Plan:
    """The trips and blocks of a rows stage: as many bands a trip as the
    L1 holds double-buffered (at most 4), `quantum` rows dividing a trip.
    Unless `wc` is given, the column block narrows (halving, whole blocks of
    32 dividing W) until one band of every operand fits."""
    if W % 32:
        raise ValueError(f"a GT4 row is whole 32-column blocks, not {W}")
    fixed = wc is not None
    wc = wc or (W if W <= MAX_BLOCK else block(W))
    while True:
        fit = _bands_fit(operands, out_kind, wc, W)
        narrower = wc // 2
        if fit >= 1 or fixed or narrower < 32 or W % narrower:
            break
        wc = narrower
    if fit < 1:
        raise ValueError(f"one band of {wc} columns does not fit a core's L1")
    b = max(1, bands or min(4, fit))
    rows = 4 * b
    while rows % quantum:
        rows += 4
    nb = -(-R // rows)
    return Plan(wc, W // wc, rows, nb, nb * rows)


def stages(pl: Plan, R: int, quantum: int = 4) -> list:
    """``[(plan, first row, rows)]``: the trips of whole `pl.rows`, then the
    rest in one trip of whole `quantum`s, then (R not a multiple of
    `quantum`) one band of `quantum` rows holding the last `rows` (None:
    every row of the stage is one of R)."""
    full, rem = divmod(R, pl.rows)
    out = []
    if full:
        out.append((Plan(pl.wc, pl.nblk, pl.rows, full, full * pl.rows), 0, None))
    whole = rem // quantum * quantum
    if whole:
        out.append((Plan(pl.wc, pl.nblk, whole, 1, whole), full * pl.rows, None))
    if rem % quantum:
        band = Plan(pl.wc, pl.nblk, quantum, 1, quantum)
        out.append((band, full * pl.rows + whole, rem % quantum))
    return out


def overrun(R: int, quantum: int = 4) -> int:
    """Rows a rows kernel over R rows writes past R."""
    return -(-R // quantum) * quantum - R


def port(o: Operand, rp: int, w: int):
    """The port and spec of an operand: a [rows, pitch] buffer covering its
    view (rows past R are the allocation's tail), a [W] row, or a [rows, 32]
    statistic."""
    if o.kind in ("full", "tile"):
        r0 = o.offset // o.pitch
        shape = (max(o.rows, r0 + rp), o.pitch)
    elif o.kind == "col":
        shape = (o.offset + w,)
    elif o.kind == "row":
        shape = (max(o.rows, o.offset + rp), 32)
    else:
        shape = (max(o.rows, o.offset + 1), 32)
    return kt.In(*shape), kt.TensorSpec.make(shape, o.dtype)


def rows_stage(
    bufs: dict,
    e,
    operands,
    out: Operand,
    pl: Plan,
    cores: int,
    row0=0,
    tile_rows: int | None = None,
):
    """Record one rows stage: `bufs` maps ``in0..`` and ``out`` to buffers
    (a `tile` operand's buffer has a ``tile=`` layout of `tile_rows`-row
    tiles); `row0` offsets every row (a batch). One phase a column block
    (the trace grows with the blocks, not the rows), its rows split over
    instances: over the cores for row-major buffers, a tile row each for a
    tiled one of several tile columns (a row walk there is affine only
    inside one tile row)."""
    for cb in range(pl.nblk):
        if pl.nblk > 1 and tile_rows is not None:
            per = tile_rows // pl.rows
            n = -(-pl.nb // per)
            at = kt.ANY if n > cores else None
            for u in kt.par(n, on=kt.vc, at=at):
                first = functools.partial(operator.mul, per)  # band of instance c
                _rows_trips(bufs, e, operands, out, pl, u, first, per, row0, cb)
            continue
        n, start, trips = split(pl.nb, cores)
        for c in kt.par(n, on=kt.vc):
            _rows_trips(bufs, e, operands, out, pl, c, start, trips(c), row0, cb)


def _rows_trips(bufs, e, operands, out, pl: Plan, c, start, trips, row0, cb) -> None:
    """Instance `c`'s trips of column block `cb` (`trips` of them from band
    `start(c)`): the prologue, then one band a trip."""
    col0 = cb * pl.wc
    pro: dict = {}
    for i, o in enumerate(operands):
        buf = bufs[f"in{i}"]
        if o.kind == "col":
            lo = o.offset + col0
            pro[i] = kt.unpack(kt.load(buf[lo : lo + pl.wc], gt4=True))
        elif o.kind == "scalar":
            at = o.offset
            pro[i] = kt.unpack(kt.load(buf[at : at + 1, :], gt4=True))

    for t in kt.range(trips):
        r = start(c) * pl.rows + t * pl.rows + row0
        _rows_block(bufs, e, operands, out, pl, r, cb, pro)


def _rows_block(bufs, e, operands, out, pl: Plan, r, cb: int, pro: dict) -> None:
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
            vals[i] = kt.load(buf[rr : rr + pl.rows, :], gt4=True)

    v = X.emit(e, vals)
    dst = bufs["out"]
    if out.kind == "row":
        if pl.nblk > 1:
            kt.store(dst[r : r + pl.rows, cb * 32 : cb * 32 + 32], v)
        else:
            kt.store(dst[r : r + pl.rows, :], v)
    else:
        rr, cc = divmod(out.offset, out.pitch)
        _store_over(dst, rr + r, cc + col0, pl.rows, pl.wc, v)


def _store_over(dst, r0, c0, rows: int, cols: int, v) -> None:
    """`v` over the [rows, cols] window of `dst` at (r0, c0): packed once,
    then stored over each of its bands (a statistic is one band of 32
    columns, a row broadcast one band of 4 rows: repeated across)."""
    if not (isinstance(v, kt.Value) and v.space == "l1"):
        if not isinstance(v, kt.Value):
            raise ValueError("a rows kernel stores a computed value, not a number")
        gt4 = v.stat or v.mode == "gt4"
        v = kt.pack(v, dst.spec.dtype, gt4=gt4)
    h, w = (v.shape[0], v.shape[-1]) if len(v.shape) == 2 else (v.shape[0], cols)
    if rows % h or cols % w:
        raise ValueError(f"a [{h}, {w}] value does not tile a [{rows}, {cols}] window")
    for i in range(0, rows, h):
        for j in range(0, cols, w):
            kt.store(dst[r0 + i : r0 + i + h, c0 + j : c0 + j + w], v)


def rows_kernel(
    e, operands: tuple, R: int, W: int, out: Operand, cores: int = 2, bands=None
):
    """A DSL kernel computing `e` over [R, W] rows into `out`. Returns
    ``(kernel, given, column blocks)``; with several blocks a reduction's
    output is [R, 32 * blocks] partials."""
    reduces = X.reduces(e)
    if out.kind == "row" and not reduces and W != 32:
        raise ValueError(
            "a statistic output comes from a row reduction or [R, 32] rows"
        )
    pl = plan(operands, out.kind, R, W, bands)
    if pl.nblk > 1 and len(reduces) > 1:
        raise ValueError("several reductions over a row wider than one block")
    parts = stages(pl, R)
    ports, given = {}, {}
    for i, o in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = port(o, R, W)
    if out.kind == "row":
        out_shape = (max(out.rows, R), 32 * pl.nblk)
    else:
        out_shape = port(out, R, W)[1].shape
    ports["out"] = kt.Out(*out_shape)
    given["out"] = kt.TensorSpec.make(out_shape)

    def body(**p):
        for sp, row0, rows in parts:
            if rows is None:
                rows_stage(p, e, operands, out, sp, cores, row0=row0)
            else:
                _tail_band(p, e, operands, out, sp, row0, rows, W)

    k = kt.Kernel.build(name("rows", repr(e), operands, R, W, out, pl), body, ports)
    return k, given, pl.nblk


def _tail_band(p, e, operands, out: Operand, pl: Plan, row0: int, rows: int, W: int):
    """The last band of a rows kernel, `rows` (< 4) of its rows real: each
    operand's rows copied by the mover into a zeroed band of scratch, the
    band computed there, its first `rows` rows copied out -- no row past R
    read or written."""
    bufs, ops = dict(p), list(operands)
    for i, o in enumerate(operands):
        if o.kind not in ("full", "tile", "row"):
            continue
        cols = 32 if o.kind == "row" else W
        rr, cc = (o.offset, 0) if o.kind == "row" else divmod(o.offset, o.pitch)
        s = kt.buffer((pl.rows, cols), o.dtype)
        kt.fill_value(s, 0)
        kt.copy(s[0:rows, :], p[f"in{i}"][rr + row0 : rr + row0 + rows, cc : cc + cols])
        bufs[f"in{i}"] = s
        ops[i] = Operand(o.kind, o.dtype, 0, 0 if o.kind == "row" else cols, pl.rows)
    cols = 32 * pl.nblk if out.kind == "row" else W
    s = kt.buffer((pl.rows, cols), out.dtype)
    bufs["out"] = s
    tail = Operand(out.kind, out.dtype, 0, 0 if out.kind == "row" else cols, pl.rows)
    rows_stage(bufs, e, tuple(ops), tail, pl, 1, row0=0)
    if out.kind == "row":
        kt.copy(p["out"][row0 : row0 + rows, :], s[0:rows, :])
    else:
        rr, cc = divmod(out.offset, out.pitch)
        kt.copy(p["out"][rr + row0 : rr + row0 + rows, cc : cc + W], s[0:rows, :])


def flat_kernel(e, dtypes: tuple, n: int, out_dtype: str = "f16", cores: int = 2):
    """`e` over `n` elements (every operand at the output's index), as
    128-value rows. Returns ``(kernel, given)``."""
    item = {"f16": 2, "f32": 4}
    words = sum(128 * item[d] // 32 for d in dtypes) + 128 * item[out_dtype] // 32
    rows = 16
    while rows > 1 and 2 * rows * words > L1_WORDS - L1_MARGIN:
        rows //= 2
    total = -(-n // 128)
    full, rem = divmod(total, rows)
    ports, given = {}, {}
    for i, d in enumerate(dtypes):
        ports[f"in{i}"] = kt.In(total, 128, dtype=d)
        given[f"in{i}"] = kt.TensorSpec.make((total, 128), d)
    ports["out"] = kt.Out(total, 128, dtype=out_dtype)
    given["out"] = kt.TensorSpec.make((total, 128), out_dtype)

    def trip(p, r, h: int) -> None:
        vals = {i: kt.load(p[f"in{i}"][r : r + h, :]) for i in range(len(dtypes))}
        kt.store(p["out"][r : r + h, :], X.emit(e, vals))

    def body(**p):
        # Whole trips over the cores, then the rest in one trip of its own:
        # no row past the output's last is read or written.
        if full:
            ncore, start, trips = split(full, cores)
            for c in kt.par(ncore, on=kt.vc):
                for t in kt.range(trips(c)):
                    trip(p, start(c) * rows + t * rows, rows)
        # The rest as whole groups of 4 rows, then 2, then 1.
        r = full * rows
        for h in (rem // 4 * 4, rem % 4 & 2, rem % 2):
            if h:
                for _ in kt.par(1, on=kt.vc):
                    for _ in kt.range(1):
                        trip(p, r, h)
                r += h

    k = kt.Kernel.build(name("flat", repr(e), dtypes, n, out_dtype), body, ports)
    return k, given


__all__ = [
    "MAX_BLOCK",
    "Operand",
    "Plan",
    "block",
    "flat_kernel",
    "name",
    "overrun",
    "plan",
    "port",
    "rows_kernel",
    "rows_stage",
    "split",
    "stages",
]
