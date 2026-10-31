"""How a read is laid out for a kernel: classification, views, binding."""

from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.launch_config import _active_scope, elide_rule
from kohakutpu.application.ktpugrad.lower import Access, Plan, Refused
from kohakutpu.application.ktpugrad.memo import memo
from kohakutpu.application.ktpugrad.run.common import (
    _addr,
    _dense,
    _half_target,
    _live,
    _mult,
    _n,
    _out_coef,
    _size,
    _window_ok,
    _wrote_flat,
)
from kohakutpu.application.ktpugrad.run.moves import _bare, _moved
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.storage import FLAT, Storage
from kohakutpu.language.numerics import fp16


@memo
def classify(acc, rows, cols) -> Operand | None:
    """`acc` against [rows, cols] (ranges outermost first): a whole row a
    pitch apart, one row, a value a row, one value; None otherwise."""
    rows, cols = _live(rows), _live(cols)
    coef = {r: c for r, c in acc.coef if U.extent(r) > 1}
    if set(coef) - set(rows) - set(cols):
        return None
    cc = [coef.get(r, 0) for r in cols]
    rc = [coef.get(r, 0) for r in rows]
    contig = cc == _mult(cols)
    nocol = not any(cc)
    pitch = None
    if any(rc):
        rm = _mult(rows)
        p = rc[-1] // rm[-1] if rm else 0
        if p and all(c == p * m for c, m in zip(rc, rm)):
            pitch = p
    norow = not any(rc)
    if contig and pitch is not None:
        return Operand("full", offset=acc.off, pitch=pitch)
    if contig and norow:
        return Operand("col", offset=acc.off)
    if nocol and pitch == 1:
        return Operand("row", offset=acc.off)
    if nocol and norow:
        return Operand("scalar", offset=acc.off)
    return None


def _zero_lanes(expr, k: int):
    """`expr` with the lanes column table `k` marks as past W stored as 0."""
    return X.op("where", X.Load(k), expr, 0.0)


def _wide(cols) -> int:
    """W' of a rows kernel over `cols`: whole 32-column blocks."""
    return -(-_size(cols) // 32) * 32


@memo
def _view(plan: Plan) -> tuple:
    """``(kind, rows, cols)`` of `plan`'s rows kernel: a reduction "along"
    the rows (rows the kept ranges, cols the reduced) or "down" them (rows
    the reduced, cols the kept: `runtime.tree`), elementwise "rows" at a
    split of the output's ranges. The fewest reads laid out wins (`_laid`),
    then whole 32-column rows, then the order listed."""
    if plan.reduce_ranges:
        red, kept = _reduce_order(plan), list(plan.out_ranges)
        views = [("along", kept, red)]
        if _live(kept):
            views.append(("down", red, kept))
    else:
        order = list(plan.out_ranges)
        views = [
            ("rows", order[:s], order[s:])
            for s in range(len(order) - 1, -1, -1)
            if _live(order[s:])
        ] or [("rows", [], order)]

    def cost(i: int) -> tuple:
        _, rows, cols = views[i]
        return (_laid(plan, rows, cols, _wide(cols)), _size(cols) % 32 != 0, i)

    return views[min(range(len(views)), key=cost)]


def _laid(plan: Plan, rows, cols, Wp: int, out: bool = True) -> int:
    """How many of `plan`'s reads (and its output window, with `out`) a rows
    kernel over [rows, cols] W' wide does not take as they are."""
    accs = list(plan.accesses)
    if out and plan.out_strides is not None:
        accs.append(_out_access(plan))
    return sum(not _takes(classify(a, rows, cols), Wp) for a in accs)


def _takes(o: Operand | None, Wp: int, start: int = 0) -> bool:
    """Whether a rows kernel W' columns wide reads `o` as it is (its buffer's
    view `start` elements in; `rows._whole_rows`): rows of whole 32-byte
    words at least W' apart, a row from a word, a value a row from a word
    (16 a word), one value."""
    if o is None:
        return False
    if o.kind == "full":
        return (
            not ((start + o.offset) % 16 or o.pitch % 16)
            and o.offset % o.pitch + Wp <= o.pitch
        )
    if o.kind in ("col", "row"):
        return not (start + o.offset) % 16
    return True


def _bind(dev, plan: Plan, a: Access, rows, cols, Wp: int, bufs, tmps) -> tuple:
    """``(address, operand, runs)`` of read `a` for a rows kernel over [rows,
    cols] W' wide: as it is where the kernel takes it (`_takes`; rows of
    values in the f16 form, a statistic's flattened), else laid out
    (`_laid_read`) over every row when it reads one and every column when it
    reads one, rows W' apart."""
    o = classify(a, rows, cols)
    off = FM.base_of(bufs[a.slot])[1] if a.slot is not None else 0
    if (
        o is not None
        and not (o.kind == "col" and _size(cols) < Wp)
        and _takes(o, Wp, off // np.dtype(F.np_dtype(a.dtype)).itemsize)
    ):
        at, o = _addr(dev, a, o, bufs)
        return at, o, []
    over_r = _live(rows) if any(a.c(r) for r in _live(rows)) else []
    over_c = _live(cols) if any(a.c(r) for r in _live(cols)) else []
    pitch = Wp if over_c else 1
    key = ("laid", id(a), id(plan))
    coef = {r: s * pitch for r, s in _dense(over_r).items()} | _dense(over_c)
    laid = Access(key, dtypes.half, tuple(coef.items()), 0)
    o = classify(laid, rows, cols)
    assert o is not None and _takes(o, Wp)
    n = _size(over_r) * pitch
    pend = _laid_pending(dev, a, [*over_r, *over_c], coef, n, bufs)
    if pend is not None:
        # Made inside the rows kernel (`forms.Pending`, `rows.run`).
        tmps.append(pend)
        return pend, o, []
    t, runs = _laid_read(dev, plan, a, over_r, over_c, pitch, bufs, key)
    tmps.append(t)
    at, o = _addr(dev, laid, o, {key: t})
    return at, o, runs


def _laid_pending(dev, a: Access, order, coef: dict, n: int, bufs) -> FM.Pending | None:
    """Read `a` as f16 over the ranges `order` into `n` values at the element
    strides `coef` (the rest 0), as a `forms.Pending` of one step; None for a
    read that is not a buffer's float values."""
    if not FM.PREPARED or a.slot is None or a.gate or not dtypes.is_float(a.dtype):
        return None
    st, off = FM.base_of(bufs[a.slot])
    dt = F.np_dtype(a.dtype)
    first = a.off + off // np.dtype(dt).itemsize
    live = [r for r in order if U.extent(r) > 1]
    if first < 0 or not live:
        return None
    walk = tuple((U.extent(r), a.c(r)) for r in live)
    dwalk = tuple((U.extent(r), coef[r]) for r in live)
    try:
        src, N, walk, first, dwalk = _f16_walk(dev, st, dt, walk, first, dwalk)
    except FM.FormError:
        return None
    m = -(-n // 16) * 16
    return FM.Pending(dev, max(32, 2 * m), (FM.Step(src, N, walk, first, dwalk, 0, m),))


def _laid_read(dev, plan: Plan, a: Access, over_r, over_c, pitch, bufs, key) -> tuple:
    """``(storage, runs)``: read `a` as f16 over ``[over_r.., over_c..]``,
    rows `pitch` apart (the columns past W unspecified) -- a table on the
    host, a buffer's by the bare-read kernels (`_bare`), spread to the pitch
    by `forms.repitch`."""
    order = [*over_r, *over_c]
    R, W = _size(over_r), _size(over_c)
    scope = _active_scope(dev)
    cache_key = None
    if scope is not None and a.slot is not None and a.slot in bufs:
        st_c, soff_c = FM.base_of(bufs[a.slot])
        cache_key = (
            st_c.addr,
            soff_c,
            tuple(U.extent(r) for r in over_r),
            tuple(U.extent(r) for r in over_c),
            tuple((U.extent(r), c) for r, c in a.coef if U.extent(r) > 1),
            a.off,
            pitch,
            a.dtype,
        )
        hit = scope.laid_cache.get(cache_key)
        if hit is not None:
            return hit, []
    t = Storage(dev, max(32, 2 * R * pitch))
    t.dtype = np.float16
    if a.slot is None:
        laid = np.zeros((R, pitch), np.float16)
        laid[:, :W] = _table_over(a, order).reshape(R, W)
        dev.write(t.addr, laid.tobytes())
        return t, []
    if (
        elide_rule("dense_repitch")
        and pitch != W
        and dtypes.is_float(a.dtype)
        and {r: c for r, c in a.coef if U.extent(r) > 1}
        == {r: c for r, c in _dense(order).items() if U.extent(r) > 1}
    ):
        s_in = bufs[a.slot]
        st_in, soff_in = FM.base_of(s_in)
        dt_in = F.np_dtype(a.dtype)
        n_in = (
            _n(st_in, dt_in) if not soff_in else s_in.nbytes // np.dtype(dt_in).itemsize
        )
        try:
            src_f16 = FM.f16(dev, s_in, dt_in, n_in) + 2 * a.off
        except FM.FormError:
            src_f16 = -1
        if src_f16 >= 0 and not (src_f16 % 32):
            if RW.MUTANT == "drop_member":
                return t, []
            shift = 32 if RW.MUTANT == "wrong_order" and R * W * 2 > 32 else 0
            dst_rep = src_f16 if RW.MUTANT == "alias_members" else t.addr
            FM.repitch(dev, src_f16 + shift, W, dst_rep, pitch, R, W)
            if scope is not None and cache_key is not None:
                scope.laid_cache[cache_key] = t
            return t, []
    dense = t if pitch == W else Storage(dev, max(32, 2 * R * W))
    try:
        runs = _bare(dev, _laid_plan(plan, key, a, order), {**bufs, key: dense})
        if runs is None:
            raise Refused(f"a read with no f16 form: strides {dict(a.coef)}")
        if dense is not t:
            src = FM.f16(dev, dense, np.float16, R * W)
            FM.repitch(dev, src, W, t.addr, pitch, R, W)
    except BaseException:
        t.free()
        raise
    finally:
        if dense is not t:
            dense.free()
    if scope is not None and cache_key is not None:
        scope.laid_cache[cache_key] = t
    return t, runs


def _place(dev, plan: Plan, bufs, kind: str, rows, cols, Wp: int, tmps) -> tuple:
    """``(address, operand, done)`` a rows kernel writes `plan`'s result to,
    `done()` finishing it (its runs): along, the output's f16 values where
    its statistic's words are its own (from a word, ending on one or the
    buffer), else a temporary moved into it; otherwise the output itself
    where its rows are whole words and W' = W, its padded-rows form when it
    is a whole float buffer, else a temporary moved into it (`_moved`: any
    window, any offset)."""
    if kind == "along":
        return _place_stat(dev, plan, bufs, rows, tmps)
    W = _size(cols)
    out_rows = rows if kind == "rows" else []
    R = _size(out_rows)
    dst, doff = FM.base_of(bufs[plan.out_slot])
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    whole = not doff and plan.out_strides is None and plan.out_n * item == dst.nbytes
    if kind != "along" and Wp != W and whole and dtypes.is_float(plan.out_dtype):
        key = FM.rows_key(W, Wp)
        f = dst.form(key, 2 * R * Wp)

        def form() -> list:
            dst.wrote(key)
            dst.dtype = F.np_dtype(plan.out_dtype)
            return []

        return f.addr, Operand("full", pitch=Wp), form
    tplan, tbufs, finish = _half_target(dev, plan, bufs)
    tdst, tdoff = FM.base_of(tbufs[tplan.out_slot])
    o = classify(_out_access(tplan), out_rows, cols)
    if Wp == W and not tdoff % 32 and o is not None and _takes(o, Wp):
        _window_ok(dev, tdst, tplan)

        def direct() -> list:
            _wrote_flat(tdst, tplan)
            finish()
            return []

        pitch = o.pitch if o.kind == "full" else Wp
        return tdst.addr, Operand("full", pitch=pitch, offset=tdoff // 2), direct
    t = Storage(dev, max(32, 2 * R * Wp))
    tmps.append(t)
    t.dtype = np.float16
    coef = {r: s * Wp for r, s in _dense(_live(out_rows)).items()} | _dense(_live(cols))

    def moved() -> list:
        runs = _moved(dev, tplan, tbufs, t, coef)
        finish()
        return runs

    return t.addr, Operand("full", pitch=Wp), moved


def _place_stat(dev, plan: Plan, bufs, rows, tmps) -> tuple:
    """`_place` of a reduction along the rows: its R values (16 a word) into
    the output's f16 values when those words are the statistic's own, else
    into a temporary moved in (`_moved`)."""
    R = _size(rows)
    tplan, tbufs, finish = _half_target(dev, plan, bufs)
    tdst, tdoff = FM.base_of(tbufs[tplan.out_slot])
    first, total = tdoff // 2, tdst.nbytes // 2
    own = not first % V.STAT_ROWS and (not R % V.STAT_ROWS or first + R == total)
    if plan.out_strides is None and own:
        _window_ok(dev, tdst, tplan)

        def stat() -> list:
            _wrote_flat(tdst, tplan)
            finish()
            return []

        return tdst.addr + tdoff, Operand("row"), stat
    t = Storage(dev, RW.stat_bytes(R))
    tmps.append(t)
    t.dtype = np.float16

    def window() -> list:
        t.wrote(FLAT)
        runs = _moved(dev, tplan, tbufs, t, _dense(_live(rows)))
        finish()
        return runs

    return t.addr, Operand("row"), window


def _out_access(plan: Plan) -> Access:
    return Access(plan.out_slot, plan.out_dtype, tuple(_out_coef(plan).items()), 0)


def _reduce_order(plan) -> list:
    """The reduced ranges, outermost first by an operand reading them all."""
    rs = list(plan.reduce_ranges)
    for a in plan.accesses:
        if all(a.c(r) for r in _live(rs)):
            return sorted(rs, key=lambda r: -a.c(r))
    return rs


def _f16_walk(dev, st: Storage, dt, walk: tuple, first: int, dwalk: tuple) -> tuple:
    """``(address, values, walk, first, dwalk)``: `walk` from element `first`
    of `st` read as f16 values (into `dwalk`) -- in its padded-rows form,
    strides remapped (a contiguous level of whole rows split in two, with
    its `dwalk` level), when that is current and no level crosses a row (no
    compaction), else in its f16 form (made if need be; `forms.FormError`
    when it cannot)."""
    key = FM.padded(st)
    if key is not None:
        _, n, np_ = key
        sw, sd = [], []
        for (c, s), (_, d) in zip(walk, dwalk):
            if first % n == 0 and s == 1 and c > n and c % n == 0:
                sw += [(c // n, n), (n, 1)]
                sd += [(c // n, n * d), (n, d)]
            else:
                sw.append((c, s))
                sd.append((c, d))
        inner = [(c, s) for c, s in sw if s % n and c > 1]
        span = first % n + sum(s * (c - 1) for c, s in inner)
        if all(s > 0 for _, s in inner) and span < n:
            f = st.forms[key]
            remap = tuple((c, s if s % n else s // n * np_) for c, s in sw)
            at = first // n * np_ + first % n
            return f.addr, f.nbytes // 2, remap, at, tuple(sd)
    N = _n(st, dt)
    return FM.f16(dev, st, dt, N), N, walk, first, dwalk


def _laid_plan(plan: Plan, key, acc: Access, order, strides=None) -> Plan:
    """The vector plan writing `acc` as f16 over the ranges `order` into
    ``bufs[key]``: dense, or at element `strides` (a window)."""
    return replace(
        plan,
        kind="vector",
        name=f"{plan.name}_l",
        out_slot=key,
        out_dtype=dtypes.half,
        out_ranges=tuple(order),
        out_shape=tuple(U.extent(r) for r in order),
        out_strides=None if strides is None else tuple(strides),
        expr=X.Load(0),
        accesses=[acc],
        reduce_ranges=(),
        reductions={},
        a=None,
        b=None,
        mnk=(),
        batch=(),
    )


def _table_over(acc: Access, order) -> np.ndarray:
    """A folded table's values (f16) over the ranges `order`, outermost
    first: each range it does not read broadcast."""
    assert all(r in order for r, c in acc.coef if c and U.extent(r) > 1)
    flat = fp16.write(acc.table).reshape(-1)
    idx = np.full([U.extent(r) for r in order], acc.off, np.int64)
    for i, r in enumerate(order):
        shape = [1] * len(order)
        shape[i] = U.extent(r)
        idx = idx + acc.c(r) * np.arange(U.extent(r)).reshape(shape)
    return np.ascontiguousarray(flat[idx])
