"""Generated vector-core kernels: an expression over operands, as a DSL kernel.

`rows_kernel`: [R, W] f16 rows (W a multiple of 32) in GT4 bands of 4. An
operand is `full` (an [R, W] view, rows a pitch apart), `tile` (the same of
a cluster's drained sub-tiles), `col` (one [W] row every row reads), `row` (a
statistic: R f16 values, 16 a word, from a word) or `scalar` (one value of
one, every row's). The output is an [R, W] view, or with a `Reduce` an [R]
statistic (an [blocks, R] one of partials: rows wider than a band's
registers go in column blocks, a reduction across them leaving partials to
fold); a statistic moves in trips of 16 rows. `flat_kernel`: N elements as
128-value rows (f16 or f32), each operand read at the output's index or one
value of it. A core's trips are a constant of the instance (`split`).
"""

import functools
import hashlib
import json
import operator
from collections import OrderedDict
from dataclasses import dataclass

from kohakutpu.application.kernels import expr as X
from kohakutpu.compiler import _rs
from kohakutpu.language import dsl as kt

#: How `split` deals trips over cores: ktpu-kernels' `vector::defaults`.
TRIPS = json.loads(_rs.VECTOR_DEFAULTS)["trips"]
L1_WORDS = 512
#: L1 words the lowering keeps free beside the operands (landing, slack).
L1_MARGIN = 16
#: Widest column block one band's registers take: 8 registers a value.
MAX_BLOCK = 256
KERNELS_KEPT = 1024
_kernels: OrderedDict = OrderedDict()


def expr_key(e) -> tuple:
    """DAG-aware structural key for an expression `e` (preserving shared nodes)."""
    nodes: list = []
    seen: dict = {}

    def walk(x) -> int:
        got = seen.get(id(x))
        if got is not None:
            return got
        if isinstance(x, X.Load):
            node = ("load", x.i)
        elif isinstance(x, X.Const):
            node = ("const", float(x.value))
        elif isinstance(x, X.Op):
            node = ("op", x.name, tuple(walk(a) for a in x.args))
        elif isinstance(x, X.Reduce):
            node = ("reduce", x.op, walk(x.arg))
        else:
            node = ("other", repr(x))
        seen[id(x)] = len(nodes)
        nodes.append(node)
        return seen[id(x)]

    root = walk(e)
    return (tuple(nodes), root)


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


def split(n: int, cores: int):
    """``(cores, start(c), trips(c))`` of `n` trips over `cores` cores, dealt
    by `TRIPS`: ``hchunk`` (core c starts at ``c * ceil(n / cores)``, the last
    the rest) or ``balanced`` (counts differ by at most one)."""
    if cores < 1 or n < 0:
        raise ValueError("split needs a positive core count and nonnegative trips")
    cores = max(1, min(cores, n))
    if cores == 1:
        return 1, (lambda c: 0), (lambda c: n)
    if TRIPS == "hchunk":
        h = -(-n // cores)
        m = -(-n // h)
        if m == 2:
            return 2, (lambda c: c * h), (lambda c: h + c * (n - 2 * h))
        return m, (lambda c: c * h), (lambda c: h + c // (m - 1) * (n - m * h))
    q, rem = divmod(n, cores)

    def start(c):
        return c * q + (c * rem + cores - 1) // cores

    return cores, start, lambda c: start(c + 1) - start(c)


def block(w: int, most: int = MAX_BLOCK) -> int:
    for wc in (256, 128, 64, 32):
        if wc <= most and w % wc == 0:
            return wc
    raise ValueError(f"{w} columns do not split into blocks of 32")


def bands_fit(operands, out_kind: str, wc: int, W: int) -> int:
    """Bands of 4 rows x `wc` columns the L1 holds double-buffered, beside
    the prologue (a phase is one column block of W)."""
    # A band of 4 rows x wc f16 columns is wc / 4 L1 words; a statistic's or
    # a scalar's at most one (16 rows a word).
    words = sum(wc // 4 for o in operands if o.kind in ("full", "tile"))
    words += sum(1 for o in operands if o.kind in ("row", "scalar"))
    words += 1 if out_kind == "row" else wc // 4
    prologue = sum(wc // 16 for o in operands if o.kind == "col")
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
    Unless `wc` is given, the column block narrows (whole blocks of 32
    dividing W) until the bands of `quantum` rows of every operand fit."""
    if W % 32:
        raise ValueError(f"a GT4 row is whole 32-column blocks, not {W}")
    need = max(1, quantum // 4)
    if wc is None:
        widest = W if W <= MAX_BLOCK else block(W)
        blocks = [w for w in range(widest, 31, -32) if W % w == 0]
        fits = (w for w in blocks if bands_fit(operands, out_kind, w, W) >= need)
        wc = next(fits, blocks[-1])
    fit = bands_fit(operands, out_kind, wc, W)
    if fit < need:
        raise ValueError(f"{need} bands of {wc} columns do not fit a core's L1")
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


def rows_kernel(
    e,
    operands: tuple,
    R: int,
    W: int,
    out: Operand,
    cores: int,
    bands=None,
):
    """A DSL kernel computing `e` over [R, W] rows into `out`. Returns
    ``(kernel, given, column blocks)``; with several blocks a reduction's
    output is [blocks, ceil(R / 16) * 16] compact partials."""
    operands = tuple(operands)
    kkey = (
        "rows",
        expr_key(e),
        operands,
        R,
        W,
        out,
        cores,
        bands,
        L1_WORDS,
        L1_MARGIN,
        MAX_BLOCK,
        STAT_ROWS,
        bands_fit,
        stages,
        port,
        split,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    reduces = X.reduces(e)
    stats = all(o.kind in ("row", "scalar") for o in operands)
    if out.kind == "row" and not reduces and not stats:
        raise ValueError("a statistic output comes from a row reduction or statistics")
    quantum = STAT_ROWS if holds_stat(operands, out.kind) else 4
    pl = plan(operands, out.kind, R, W, bands, quantum=quantum)
    if pl.nblk > 1 and len(reduces) > 1:
        raise ValueError("several reductions over a row wider than one block")
    parts = stages(pl, R, quantum)
    ports, given = {}, {}
    for i, o in enumerate(operands):
        ports[f"in{i}"], given[f"in{i}"] = port(o, R, W)
    if out.kind == "row":
        rows = max(out.rows, stat_words(R) * STAT_ROWS)
        out_shape = (pl.nblk, rows) if pl.nblk > 1 else (rows,)
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
    got = (k, given, pl.nblk)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


def _tail_band(p, e, operands, out: Operand, pl: Plan, row0: int, rows: int, W: int):
    """The last band of a rows kernel, `rows` (< its `pl.rows`) of its rows
    real: each operand's rows copied by the mover into a zeroed band of
    scratch, the band computed there, its first `rows` rows copied out -- no
    row past R read or written, but a statistic's: its word, whole."""
    bufs, ops = dict(p), list(operands)
    for i, o in enumerate(operands):
        if o.kind == "row":
            s = kt.buffer((pl.rows,), o.dtype)
            at = o.offset + row0
            kt.copy(s[0 : pl.rows], p[f"in{i}"][at : at + pl.rows])
            bufs[f"in{i}"] = s
            ops[i] = Operand("row", o.dtype, 0, 0, pl.rows)
            continue
        if o.kind not in ("full", "tile"):
            continue
        rr, cc = divmod(o.offset, o.pitch)
        s = kt.buffer((pl.rows, W), o.dtype)
        kt.fill_value(s, 0)
        kt.copy(s[0:rows, :], p[f"in{i}"][rr + row0 : rr + row0 + rows, cc : cc + W])
        bufs[f"in{i}"] = s
        ops[i] = Operand(o.kind, o.dtype, 0, W, pl.rows)
    if out.kind == "row":
        shape = (pl.nblk, pl.rows) if pl.nblk > 1 else (pl.rows,)
        s = kt.buffer(shape, out.dtype)
        tail = Operand("row", out.dtype, 0, 0, pl.rows)
    else:
        s = kt.buffer((pl.rows, W), out.dtype)
        tail = Operand(out.kind, out.dtype, 0, W, pl.rows)
    bufs["out"] = s
    rows_stage(bufs, e, tuple(ops), tail, pl, 1, row0=0)
    if out.kind == "row" and pl.nblk > 1:
        kt.copy(p["out"][:, row0 : row0 + pl.rows], s[:, 0 : pl.rows])
    elif out.kind == "row":
        kt.copy(p["out"][row0 : row0 + pl.rows], s[0 : pl.rows])
    else:
        rr, cc = divmod(out.offset, out.pitch)
        kt.copy(p["out"][rr + row0 : rr + row0 + rows, cc : cc + W], s[0:rows, :])


def flat_kernel(
    e,
    dtypes: tuple,
    n: int,
    out_dtype: str,
    cores: int,
    scalars: tuple = (),
):
    """`e` over `n` elements, whole 128-value rows (every operand at the
    output's index, but ``(i, lane)`` of `scalars`: f16 value `lane` of operand
    i's word, every element's). Returns ``(kernel, given)``."""
    if n % 128:
        raise ValueError(f"{n} elements are not whole 128-value rows")
    dtypes = tuple(dtypes)
    scalars = tuple(scalars)
    kkey = (
        "flat",
        expr_key(e),
        dtypes,
        n,
        out_dtype,
        cores,
        scalars,
        L1_WORDS,
        L1_MARGIN,
        split,
        kt.Kernel.build,
    )
    hit = _kernels.get(kkey)
    if hit is not None:
        _kernels.move_to_end(kkey)
        return hit
    item = {"f16": 2, "f32": 4}
    lanes = dict(scalars)
    words = sum(128 * item[d] // 32 for i, d in enumerate(dtypes) if i not in lanes)
    words += len(lanes) + 128 * item[out_dtype] // 32
    rows = 16
    while rows > 1 and 2 * rows * words > L1_WORDS - L1_MARGIN:
        rows //= 2
    total = n // 128
    full, rem = divmod(total, rows)
    ports, given = {}, {}
    for i, d in enumerate(dtypes):
        shape = (16,) if i in lanes else (total, 128)
        ports[f"in{i}"] = kt.In(*shape, dtype=d)
        given[f"in{i}"] = kt.TensorSpec.make(shape, d)
    ports["out"] = kt.Out(total, 128, dtype=out_dtype)
    given["out"] = kt.TensorSpec.make((total, 128), out_dtype)

    def trip(p, r, h: int) -> None:
        vals = {}
        for i in range(len(dtypes)):
            if i in lanes:
                vals[i] = kt.unpack(kt.load(p[f"in{i}"][0:16]), lane=lanes[i])
            else:
                vals[i] = kt.load(p[f"in{i}"][r : r + h, :])
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

    key = name("flat", repr(e), dtypes, n, out_dtype, tuple(sorted(lanes.items())))
    got = (kt.Kernel.build(key, body, ports), given)
    if KERNELS_KEPT > 0:
        _kernels[kkey] = got
        while len(_kernels) > KERNELS_KEPT:
            _kernels.popitem(last=False)
    return got


__all__ = [
    "MAX_BLOCK",
    "STAT_ROWS",
    "Operand",
    "Plan",
    "bands_fit",
    "block",
    "flat_kernel",
    "holds_stat",
    "name",
    "overrun",
    "plan",
    "port",
    "rows_kernel",
    "rows_stage",
    "split",
    "stages",
    "stat_words",
]
