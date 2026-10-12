"""Running a plan on the device: each read classified as a `vector.Operand`
against the kernel's rows and columns, its form made (`runtime.forms`), the
generated DSL kernel compiled (cached) and run, the output's form recorded.

Where the forms are is known only when the kernel runs (a statistic a
reduction left is not row-major bytes), so the last choices are made here:
a kernel whose operands are statistics runs over their [n, 32] forms, a
row-major one over the bytes.
"""

import itertools
from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import convert as C
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import transpose as TP
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import lower as L
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.lower import Access, Plan, Refused
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import tree as TR
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView


def _reform(dev, plan: Plan, bufs: dict) -> tuple:
    """Reads of a buffer held as padded rows (rows of n, np apart): each read
    through an alias of that form with its strides remapped (a row step of n
    is one of np); a read that crosses a row stays as it is (and is refused
    where its row-major bytes are wanted)."""
    bufs = dict(bufs)
    accesses = []
    for a in plan.accesses:
        s = bufs.get(a.slot) if a.slot is not None else None
        st, off = FM.base_of(s) if s is not None else (None, 0)
        key = FM.padded(st) if st is not None and not off else None
        if key is None or a.slot == plan.out_slot or not dtypes.is_float(a.dtype):
            accesses.append(a)
            continue
        _, n, np_ = key
        inner = [(r, c) for r, c in a.coef if c % n and U.extent(r) > 1]
        span = a.off % n + sum(c * (U.extent(r) - 1) for r, c in inner)
        if any(c < 0 for _, c in inner) or span >= n:
            accesses.append(a)
            continue
        coef = tuple((r, c if c % n else c // n * np_) for r, c in a.coef)
        off = a.off // n * np_ + a.off % n
        alias = ("rows", a.slot)
        if alias not in bufs:
            assert st is not None
            f = st.forms[key]
            bufs[alias] = Storage(dev, f.nbytes, addr=f.addr, owned=False)
        # The form holds f16 values whatever the buffer's element type.
        accesses.append(replace(a, slot=alias, coef=coef, off=off, dtype=dtypes.half))
    return replace(plan, accesses=accesses), bufs


def execute(dev, plan: Plan, bufs: dict):
    """Run `plan` with `bufs` (param slot -> storage); returns the runs."""
    if plan.kind in ("vector", "copy"):
        plan, bufs = _reform(dev, plan, bufs)
        if plan.kind == "copy" and any(
            a.slot != plan.out_slot and isinstance(a.slot, tuple) for a in plan.accesses
        ):
            plan = replace(plan, kind="vector")
    out = bufs[plan.out_slot]
    ndt = F.np_dtype(plan.out_dtype)
    match plan.kind:
        case "const":
            st, off = FM.base_of(out)
            raw = np.ascontiguousarray(plan.value, ndt).tobytes()
            dev.write(st.addr + off, raw)
            st.wrote(FLAT)
            st.host = raw if not off else None
            st.dtype = ndt
            return []
        case "copy":
            return _copy(dev, plan, bufs)
        case "vector":
            return _vector(dev, plan, bufs)
        case "matmul":
            return _matmul(dev, plan, bufs)
    raise Refused(f"no plan kind {plan.kind}")


# ------------------------------------------------------------- operands
def _live(rs):
    return [r for r in rs if U.extent(r) > 1]


def _mult(rs) -> list:
    out, step = [], 1
    for r in reversed(rs):
        out.insert(0, step)
        step *= U.extent(r)
    return out


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


def _n(st, dtype) -> int:
    return st.nbytes // np.dtype(dtype).itemsize


def _addr(dev, acc, o: Operand, bufs) -> tuple:
    """``(address, operand)`` of a read in the form its kind wants."""
    if acc.slot is None:
        at = FM.table(dev, acc.table)
        if o.kind == "row":
            return _table_rowstat(dev, acc), o
        return at, o
    s = bufs[acc.slot]
    dt = F.np_dtype(acc.dtype)
    st, off = FM.base_of(s)
    if o.kind == "row" or (
        o.kind == "scalar" and FLAT not in st.valid and FM.F16 not in st.valid
    ):
        if off:
            raise Refused("a statistic read from a sub-buffer")
        return FM.rowstat(dev, st, dt, _n(st, dt)), o
    if o.kind == "scalar":
        # One value: its row of the statistic form.
        return FM.rowstat(dev, st, dt, _n(st, dt)), o
    return FM.f16(dev, s, dt, _n(st, dt) if not off else s.nbytes // dt.itemsize), o


def _table_rowstat(dev, acc) -> int:
    a = np.asarray(acc.table, np.float16).reshape(-1)
    return FM.table(dev, np.repeat(a[:, None], 32, 1))


def _out(dev, plan: Plan, bufs, stat: bool) -> tuple:
    """``(address, key)`` the kernel writes the output to."""
    s = bufs[plan.out_slot]
    st, off = FM.base_of(s)
    n = plan.out_n
    if stat:
        # A segment's statistic: its rows of the whole buffer's [N, 32] form,
        # with room for a statistic-space kernel's last band (4 rows a value).
        item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
        total = max(n, st.nbytes // item)
        f = st.form(FM.ROWSTAT, max(RW.stat_bytes(total), 4 * 64 * total))
        return f.addr + (off // item) * 64, FM.ROWSTAT
    if np.dtype(F.np_dtype(plan.out_dtype)) == np.float16:
        return st.addr + off, FLAT
    if off:
        raise Refused(f"a {plan.out_dtype} result into a sub-buffer")
    return st.form(FM.F16, 2 * n).addr, FM.F16


def _wrote(dev, plan: Plan, bufs, key) -> None:
    st, _ = FM.base_of(bufs[plan.out_slot])
    st.wrote(key)
    st.dtype = F.np_dtype(plan.out_dtype)


def _stat_only(dev, acc, bufs) -> bool:
    if acc.slot is None:
        return False
    st, _ = FM.base_of(bufs[acc.slot])
    return FM.ROWSTAT in st.valid and FLAT not in st.valid and FM.F16 not in st.valid


# ----------------------------------------------------------------- copy
def _copy(dev, plan: Plan, bufs) -> list:
    acc = plan.accesses[0]
    src, dst = bufs[acc.slot], bufs[plan.out_slot]
    sst, soff = FM.base_of(src)
    dst_, doff = FM.base_of(dst)
    key = next((k for k in (FLAT, FM.F16, FM.ROWSTAT) if k in sst.valid), None)
    if key is None:
        raise Refused("a copy of a buffer with no current form")
    if key != FLAT and (soff or doff):
        raise Refused("a copy of a sub-buffer's statistic")
    if key == FLAT:
        n = plan.out_n * np.dtype(F.np_dtype(plan.out_dtype)).itemsize
        a, b = sst.addr + soff, dst_.addr + doff
        if a % 32 or b % 32:
            raise Refused("a copy between sub-buffers not on 32-byte words")
    else:
        f = sst.forms[key]
        n = f.nbytes
        a, b = f.addr, dst_.form(key, n).addr
    k, given = C.copy_kernel(n)
    run = FM.launch(
        dev, k, given, {"src": a, "dst": b}, kind="copy", info={"plan": plan.name}
    )
    dst_.wrote(key)
    dst_.dtype = F.np_dtype(plan.out_dtype)
    if key == FLAT and sst.host is not None and not soff and not doff:
        dst_.host = sst.host
    return [run]


# --------------------------------------------------------------- vector
def _all_reduces(e, out=None) -> list:
    """Every `Reduce` in `e`, inner ones too."""
    out = [] if out is None else out
    if isinstance(e, X.Reduce):
        out.append(e)
        _all_reduces(e.arg, out)
    elif isinstance(e, X.Op):
        for a in e.args:
            _all_reduces(a, out)
    return out


def _prune(e, accesses: list) -> tuple:
    """`e` reading only the accesses it uses, renumbered in order of use."""
    used: dict = {}

    def walk(x):
        if isinstance(x, X.Load):
            if x.i not in used:
                used[x.i] = len(used)
            return X.Load(used[x.i])
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(walk(a) for a in x.args))
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, walk(x.arg))
            remap[id(new)] = id(x)
            return new
        return x

    remap: dict = {}
    e2 = walk(e)
    acc = [None] * len(used)
    for old, new in used.items():
        acc[new] = accesses[old]
    return e2, acc, remap


def _leaf_reduce(plan: Plan):
    """The first reduction with none inside it."""
    return next(r for r in _all_reduces(plan.expr) if not _all_reduces(r.arg))


#: Ranges up to which every order of a leaf's temporary is tried.
ORDER_TRIES = 6


def _leaf(plan: Plan, leaf) -> tuple:
    """``(plan, ranges, clusters)``: `leaf` alone into a dense f16 temporary
    over the ranges outside its reduction. A product's leaf is laid out so
    the clusters take it (`_contraction` of the order, adjacent runs
    coalesced: [batch, M, N]); otherwise outermost first by read stride."""
    rin = plan.reductions[id(leaf)]
    sub_e, sub_acc, remap = _prune(leaf, plan.accesses)
    outside = []
    for a in sub_acc:
        for r, _ in a.coef:
            if U.extent(r) > 1 and r not in rin and r not in outside:
                outside.append(r)
    stride = {r: max(a.c(r) for a in sub_acc) for r in outside}
    rank = {r: i for i, r in enumerate(plan.out_ranges)}
    outside.sort(key=lambda r: (-stride[r], rank.get(r, len(rank)), r.arg[0]))

    base = replace(
        plan,
        kind="vector",
        name=f"{plan.name}_r",
        out_dtype=dtypes.half,
        out_strides=None,
        expr=sub_e,
        accesses=sub_acc,
        reduce_ranges=tuple(rin),
        reductions={k: rin for k in remap},
    )

    def laid(order) -> Plan:
        rs = tuple(order)
        return replace(base, out_ranges=rs, out_shape=tuple(U.extent(r) for r in rs))

    orders = itertools.permutations(outside) if len(outside) <= ORDER_TRIES else []
    for order in orders:
        probe = _coalesce(laid(order))
        if _contraction(probe) is not None:
            return probe, list(order), True
    return laid(outside), outside, False


def _epilogue_contraction(plan: Plan) -> bool:
    """A product under elementwise math the planner did not see as one (its
    output permuted, a bias beside it): the clusters take its leaf."""
    if not plan.reduce_ranges or isinstance(plan.expr, X.Reduce):
        return False
    leaf = _leaf_reduce(plan)
    if len(_all_reduces(plan.expr)) != 1:
        return False
    return _leaf(plan, leaf)[2]


def _materialise(dev, plan: Plan, bufs) -> list:
    """Reductions over different range sets: an innermost one first, into a
    temporary over the ranges outside it (a statistic of each), read back by
    the rest as an operand. Recursive until one set is left."""
    leaf = _leaf_reduce(plan)
    key = ("tmp", id(leaf))
    sub, outside, _ = _leaf(plan, leaf)
    sub = replace(sub, out_slot=key)
    n = int(np.prod([U.extent(r) for r in outside])) if outside else 1
    tmp = Storage(dev, max(32, 2 * n))
    mult = _mult(outside)
    read = Access(key, dtypes.half, tuple(zip(outside, mult)), 0)
    reductions = {}

    def swap(x):
        if x is leaf:
            return X.Load(len(plan.accesses))
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(swap(a) for a in x.args))
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, swap(x.arg))
            reductions[id(new)] = plan.reductions[id(x)]
            return new
        return x

    outer_e = swap(plan.expr)
    outer_e, outer_acc, remap2 = _prune(outer_e, [*plan.accesses, read])
    left = {k: reductions[v] for k, v in remap2.items()}
    rr = tuple(dict.fromkeys(r for rs in left.values() for r in rs))
    outer = replace(
        plan, expr=outer_e, accesses=outer_acc, reductions=left, reduce_ranges=rr
    )
    sub_bufs = {**bufs, key: tmp}
    try:
        runs = _vector(dev, sub, sub_bufs)
        runs += _vector(dev, outer, sub_bufs)
    finally:
        tmp.free()
    return runs


def _split_reduction(plan: Plan):
    """One reduction over ranges both inside and outside the kept ones, as
    two: the inner ranges (finer than every kept one) reduced first, then
    the outer (sum / max / product compose); None when nothing splits."""
    reds = _all_reduces(plan.expr)
    if len(reds) != 1:
        return None
    red = reds[0]
    rs = [r for r in plan.reductions[id(red)] if U.extent(r) > 1]
    reader = next((a for a in plan.accesses if all(a.c(r) for r in rs)), None)
    if reader is None:
        return None
    kept = [reader.c(r) for r in plan.out_ranges if U.extent(r) > 1 and reader.c(r)]
    finest = min(kept) if kept else None
    inner = tuple(r for r in rs if finest is None or reader.c(r) < finest)
    outer = tuple(r for r in rs if r not in inner)
    if (not inner or not outer) and len(rs) > 1:
        # Reduced and kept ranges interleave: the outermost reduced one last.
        top = max(rs, key=reader.c)
        inner, outer = tuple(r for r in rs if r is not top), (top,)
    if not inner or not outer:
        return None
    first = X.Reduce(red.op, red.arg)
    second = X.Reduce(red.op, first)

    def swap(x):
        if x is red:
            return second
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(swap(a) for a in x.args))
        return x

    return replace(
        plan, expr=swap(plan.expr), reductions={id(first): inner, id(second): outer}
    )


def _column(dev, plan: Plan, bufs) -> list:
    """A reduction over outer ranges: the kept ranges are the columns, the
    reduced ones the rows of a halving tree (`runtime.tree`)."""
    rows, cols = _reduce_order(plan), list(plan.out_ranges)
    got = [classify(a, rows, cols) for a in plan.accesses]
    bad = [a for a, o in zip(plan.accesses, got) if o is None]
    if bad:
        split = _split_reduction(plan)
        if split is not None:
            return _materialise(dev, split, bufs)
        raise Refused(
            "an operand read transposed against the reduction: strides "
            f"{dict(bad[0].coef)}"
        )
    ops = [o for o in got if o is not None]
    R = int(np.prod([U.extent(r) for r in rows])) if rows else 1
    W = int(np.prod([U.extent(r) for r in _live(cols)])) if _live(cols) else 1
    # A statistic read as a value a row (or one value) is read through its
    # [n, 32] form as it stands; only one read as rows of values matters.
    stat_in = any(
        _stat_only(dev, a, bufs) and o.kind in ("full", "col")
        for a, o in zip(plan.accesses, ops)
    )
    if (
        stat_in
        and W > 1
        and not W % 32
        and all(
            o.kind == "full"
            for a, o in zip(plan.accesses, ops)
            if _stat_only(dev, a, bufs) and o.kind in ("full", "col")
        )
    ):
        # Rows of values held as a statistic: their row-major bytes made on
        # card (`forms.flatten`), then reduced W wide, not 32 W.
        for a in plan.accesses:
            if _stat_only(dev, a, bufs):
                st, _ = FM.base_of(bufs[a.slot])
                FM.flatten(dev, st, _n(st, F.np_dtype(a.dtype)))
        stat_in = False
    if W > 1 and W % 32 and not stat_in:
        runs = _column_padded(dev, plan, bufs, ops, R, W)
        if runs is not None:
            return runs
    # Statistic space (any width): every value its [n, 32] form's row, so a kept
    # value is 32 columns and the result is the output's statistic form.
    stat = W == 1 or W % 32 or stat_in
    binds, operands = {}, []
    for i, (a, o) in enumerate(zip(plan.accesses, ops)):
        if not stat:
            at, o = _addr(dev, a, o, bufs)
            binds[f"in{i}"] = at
            operands.append(o)
            continue
        if a.slot is None:
            binds[f"in{i}"] = _table_rowstat(dev, a)
        else:
            st, off = FM.base_of(bufs[a.slot])
            if off:
                raise Refused("a statistic of a sub-buffer")
            dt = F.np_dtype(a.dtype)
            binds[f"in{i}"] = FM.rowstat(dev, st, dt, _n(st, dt))
        if o.kind == "full":
            o = Operand("full", pitch=o.pitch * 32, offset=o.offset * 32, rows=R)
        elif o.kind == "col":
            o = Operand("col", offset=o.offset * 32)
        operands.append(o)
    width = W * 32 if stat else W
    if width % 32:
        raise Refused(f"a reduction down rows of {W}: whole 32-column blocks")
    out_at, key = _out(dev, plan, bufs, bool(stat))
    out = Operand("full", pitch=width)
    try:
        runs = TR.column(
            dev,
            plan.expr,
            operands,
            binds,
            R,
            width,
            out,
            out_at,
            info={"plan": plan.name, "tree": R},
        )
    except RW.RowsError as e:
        raise Refused(str(e)) from None
    _wrote(dev, plan, bufs, key)
    return runs


def _column_padded(dev, plan: Plan, bufs, ops, R: int, W: int) -> list | None:
    """A reduction down rows of W kept values, W not whole 32-column blocks:
    the tree runs W' = ceil32(W) wide (rows read W' apart, laid out so by the
    mover when they are closer), its row into the output's padded-rows form.
    None when a read or the output does not allow it."""
    Wp = -(-W // 32) * 32
    dst, doff = FM.base_of(bufs[plan.out_slot])
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    if doff or item * plan.out_n != dst.nbytes or not dtypes.is_float(plan.out_dtype):
        return None
    binds, operands, tmps = {}, [], []
    try:
        for i, (a, o) in enumerate(zip(plan.accesses, ops)):
            if a.slot is None:
                if o.kind != "scalar" and a.c(plan.out_ranges[-1]):
                    return None
                at, o = _addr(dev, a, o, bufs)
            elif o.kind == "full" and o.pitch < Wp:
                if (2 * W) % 32 or o.offset % 16 or o.pitch % 16:
                    return None
                src = FM.f16(
                    dev, bufs[a.slot], F.np_dtype(a.dtype), _n(*_st_dt(bufs[a.slot], a))
                )
                t = Storage(dev, 2 * R * Wp)
                tmps.append(t)
                FM.repitch(dev, src + 2 * o.offset, o.pitch, t.addr, Wp, R, W)
                at, o = t.addr, Operand("full", pitch=Wp, rows=R)
            else:
                at, o = _addr(dev, a, o, bufs)
            binds[f"in{i}"] = at
            operands.append(o)
        key = FM.rows_key(W, Wp)
        f = dst.form(key, 2 * Wp)
        try:
            runs = TR.column(
                dev,
                plan.expr,
                operands,
                binds,
                R,
                Wp,
                Operand("full", pitch=Wp),
                f.addr,
                info={"plan": plan.name, "tree": R, "narrow": W},
            )
        except RW.RowsError as e:
            raise Refused(str(e)) from None
        dst.wrote(key)
        dst.dtype = F.np_dtype(plan.out_dtype)
        return runs
    finally:
        for t in tmps:
            t.free()


#: Most launches a segmented kernel takes (one an index of its fixed ranges).
MAX_SEGMENTS = 64


def _views(plan: Plan) -> bool:
    """Whether some row view reads every operand: along the rows, down them
    (a reduction) or elementwise."""
    if plan.reduce_ranges:
        for rows, cols in (
            (list(plan.out_ranges), _reduce_order(plan)),
            (_reduce_order(plan), list(plan.out_ranges)),
        ):
            if all(classify(a, rows, cols) is not None for a in plan.accesses):
                return True
        return False
    try:
        _split(plan)
        return True
    except Refused:
        return False


def _fix(plan: Plan, fixed: list, idx: tuple, bufs: dict):
    """`plan` with the ranges `fixed` held at `idx`: the reads' offsets
    moved, the output a view of its block."""
    at = dict(zip(fixed, idx))
    accesses = []
    for a in plan.accesses:
        off = a.off + sum(c * at[r] for r, c in a.coef if r in at)
        coef = tuple((r, c) for r, c in a.coef if r not in at)
        table = a.table
        if a.slot is None and any(r in at for r, _ in a.coef):
            # A folded table (row-major, outermost range first): its slice at
            # the held indices, dense again.
            rs = [r for r, _ in sorted(a.coef, key=lambda rc: -rc[1])]
            sl = tuple(at[r] if r in at else slice(None) for r in rs)
            table = np.ascontiguousarray(np.asarray(a.table)[sl])
            keep = [r for r in rs if r not in at]
            coef = tuple(zip(keep, _mult(keep)))
            off = 0
        accesses.append(replace(a, off=off, coef=coef, table=table))
    out = [r for r in plan.out_ranges if r not in at]
    mult = dict(zip(plan.out_ranges, _mult(plan.out_ranges)))
    first = sum(mult[r] * at[r] for r in fixed)
    n = int(np.prod([U.extent(r) for r in out])) if out else 1
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    st, off = FM.base_of(bufs[plan.out_slot])
    view = StorageView(st, off + first * item, n * item)
    sub = replace(
        plan,
        out_ranges=tuple(out),
        out_shape=tuple(U.extent(r) for r in out),
        accesses=accesses,
    )
    return sub, {**bufs, plan.out_slot: view}


def _segmented(dev, plan: Plan, bufs) -> list | None:
    """No row view of the whole kernel: the smallest outermost set of kept
    ranges whose every index leaves one, a launch an index; None if none."""
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    for k in range(1, len(live) + 1):
        fixed = live[:k]
        count = int(np.prod([U.extent(r) for r in fixed]))
        if count > MAX_SEGMENTS:
            return None
        trial, _ = _fix(plan, fixed, (0,) * k, bufs)
        if not _views(trial):
            continue
        runs = []
        for idx in itertools.product(*(range(U.extent(r)) for r in fixed)):
            sub, sub_bufs = _fix(plan, fixed, idx, bufs)
            runs += _vector(dev, sub, sub_bufs)
        return runs
    return None


#: Reads a vector-core kernel walks beside its output (8 memory views).
MAX_READS = 7


def _loads(e) -> set:
    if isinstance(e, X.Load):
        return {e.i}
    if isinstance(e, X.Op):
        return set().union(*(_loads(a) for a in e.args))
    if isinstance(e, X.Reduce):
        return _loads(e.arg)
    return set()


def _narrow(dev, plan: Plan, bufs) -> list:
    """More reads than a core walks: the subexpression (no reduction in it)
    reading the most reads that fit, materialised first into an f16
    temporary over the ranges its reads walk, read back as one."""
    best = None
    stack = [plan.expr]
    while stack:
        x = stack.pop()
        if isinstance(x, X.Op):
            n = len(_loads(x))
            if (
                2 <= n <= MAX_READS
                and not _all_reduces(x)
                and (best is None or n > best[0])
            ):
                best = (n, x)
            stack += list(x.args)
        elif isinstance(x, X.Reduce):
            stack.append(x.arg)
    if best is None:
        raise Refused(f"{len(plan.accesses)} reads with no part of at most {MAX_READS}")
    sub_e, sub_acc, _ = _prune(best[1], plan.accesses)
    live = {r for a in sub_acc for r, _ in a.coef if U.extent(r) > 1}
    order = [*plan.out_ranges, *_reduce_order(plan)]
    rs = [r for r in order if r in live]
    key = ("narrow", id(best[1]))
    n = int(np.prod([U.extent(r) for r in rs])) if rs else 1
    tmp = Storage(dev, max(32, 2 * n))
    sub = replace(
        plan,
        name=f"{plan.name}_n",
        out_slot=key,
        out_dtype=dtypes.half,
        out_shape=tuple(U.extent(r) for r in rs),
        out_ranges=tuple(rs),
        expr=sub_e,
        accesses=sub_acc,
        reduce_ranges=(),
        reductions={},
    )
    read = Access(key, dtypes.half, tuple(zip(rs, _mult(rs))), 0)
    reductions = {}

    def swap(x):
        if x is best[1]:
            return X.Load(len(plan.accesses))
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(swap(a) for a in x.args))
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, swap(x.arg))
            reductions[id(new)] = plan.reductions[id(x)]
            return new
        return x

    outer_e, outer_acc, remap = _prune(swap(plan.expr), [*plan.accesses, read])
    outer = replace(
        plan,
        expr=outer_e,
        accesses=outer_acc,
        reductions={k: reductions[v] for k, v in remap.items()},
    )
    sub_bufs = {**bufs, key: tmp}
    try:
        return _vector(dev, sub, sub_bufs) + _vector(dev, outer, sub_bufs)
    finally:
        tmp.free()


def _walk(plan: Plan, coef: dict, item: int) -> list | None:
    """``[(count, byte stride)]`` of element strides `coef` over the output's
    ranges, adjacent levels merged, the last whole 32-byte words; None if
    not words."""
    lv = [
        (U.extent(r), coef.get(r, 0) * item) for r in plan.out_ranges if U.extent(r) > 1
    ]
    merged: list = []
    for n, s in reversed(lv):
        if merged and s == merged[-1][0] * merged[-1][1]:
            m, t = merged.pop()
            merged.append((n * m, t))
        else:
            merged.append((n, s))
    merged.reverse()
    if not merged:
        merged = [(1, item)]
    n, s = merged[-1]
    if s != item or (n * item) % 32 or any(t % 32 for _, t in merged[:-1]):
        return None
    return [*merged[:-1], (n * item // 32, 32)]


def _move(dev, plan: Plan, bufs) -> list | None:
    """A read stored as it is, walked in whole words (a slice, a permute of
    the outer axes, a flip of one, a broadcast row): one mover copy."""
    acc = plan.accesses[0]
    src = _source(dev, plan, acc, bufs)
    if src is None:
        return None
    base, nbytes, item, soff = src
    dst, doff = FM.base_of(bufs[plan.out_slot])
    levels = _walk(plan, dict(acc.coef), item)
    dlevels = _walk(plan, _out_coef(plan), item)
    at = soff + acc.off * item
    if levels is None or dlevels is None or at % 32 or doff % 32 or at < 0:
        return None
    _window_ok(dst, plan)
    k, given = C.walk_kernel(
        nbytes, tuple(levels), at, dst.nbytes, tuple(dlevels), doff
    )
    run = FM.launch(
        dev,
        k,
        given,
        {"src": base, "dst": dst.addr},
        kind="move",
        info={"plan": plan.name},
    )
    _wrote_flat(dst, plan)
    return [run]


def _source(dev, plan: Plan, acc: Access, bufs) -> tuple | None:
    """``(address, bytes, element size, byte offset)`` of the values a mover
    walks for `acc`: the buffer's own bytes when the output keeps its type,
    else (an f16 output of float data) its f16 form, made if need be."""
    if acc.slot is None:
        return None
    s = bufs[acc.slot]
    st, off = FM.base_of(s)
    dt = F.np_dtype(acc.dtype)
    if acc.dtype == plan.out_dtype and FLAT in st.valid:
        return st.addr, st.nbytes, np.dtype(dt).itemsize, off
    if plan.out_dtype != dtypes.half or not dtypes.is_float(acc.dtype):
        return None
    try:
        addr = FM.f16(dev, s, dt, _n(st, dt))
    except FM.FormError:
        return None
    if np.dtype(dt) == np.float16:
        return st.addr, st.nbytes, 2, off
    return addr, 2 * _n(st, dt), 2, 0


def _dense(rs) -> dict:
    return dict(zip(rs, _mult(rs)))


def _transpose(dev, plan: Plan, bufs) -> list | None:
    """A read stored with its innermost axis moved (a transpose): the mover
    puts the two axes last, `kernels.transpose` swaps them on card
    (exact), the mover moves the result into place; an identity move is
    skipped. None when it is not one (or not f16)."""
    acc = plan.accesses[0]
    if plan.out_dtype != dtypes.half:
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if len(live) < 2:
        return None
    r_in = live[-1]
    r_s = next((r for r in live if acc.c(r) == 1), None)
    if r_s is None or r_s is r_in:
        return None
    A, B = U.extent(r_in), U.extent(r_s)
    if A % 16 or B % 16:
        return None  # sides not of whole words: `_gather`
    others = [r for r in live if r not in (r_in, r_s)]
    nb = int(np.prod([U.extent(r) for r in others])) if others else 1
    n = nb * A * B
    runs: list = []
    found = _source(dev, plan, acc, bufs)
    if found is None:
        return None
    base, _, item, off = found
    order1 = [*others, r_in, r_s]
    tmps = []
    try:
        if (
            {r: c for r, c in acc.coef if U.extent(r) > 1} == _dense(order1)
            and item == 2
            and not (off + 2 * acc.off) % 32
        ):
            src = base + off + 2 * acc.off
        else:
            t1 = Storage(dev, 2 * n)
            tmps.append(t1)
            key = ("tr1", id(plan))
            sub = replace(
                plan,
                name=f"{plan.name}_p",
                out_slot=key,
                out_dtype=dtypes.half,
                out_ranges=tuple(order1),
                out_shape=tuple(U.extent(r) for r in order1),
                out_strides=None,
            )
            got = _move(dev, sub, {**bufs, key: t1})
            if got is None:
                raise Refused("a transpose whose read is not whole words before it")
            runs += got
            src = t1.addr
        order2 = [*others, r_s, r_in]
        dst, doff = FM.base_of(bufs[plan.out_slot])
        oc = {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1}
        direct = oc == _dense(order2) and not doff % 32
        if direct:
            _window_ok(dst, plan)
            out = dst.addr + doff
        else:
            t2 = Storage(dev, 2 * n)
            tmps.append(t2)
            out = t2.addr
        k, given = TP.transpose_kernel(nb, A, B, dev.machine.count("VC"))
        runs.append(
            FM.launch(
                dev,
                k,
                given,
                {"x": src, "mask": FM.table(dev, TP.masks()), "y": out},
                kind="transpose",
                info={"plan": plan.name},
            )
        )
        if direct:
            _wrote_flat(dst, plan)
            return runs
        key2 = ("tr2", id(plan))
        read = Access(key2, dtypes.half, tuple(_dense(order2).items()), 0)
        got = _move(
            dev,
            replace(plan, expr=X.Load(0), accesses=[read]),
            {**bufs, key2: tmps[-1]},
        )
        if got is None:
            raise Refused("a transpose whose output is not whole words after it")
        return runs + got
    finally:
        for t in tmps:
            t.free()


#: Levels one mover walk holds (`kohakuaccel...package.mover.NDIM`).
WALK_LEVELS = 6


def _gather(dev, plan: Plan, bufs) -> list | None:
    """A read no word walk takes (a transpose or a flip of rows not of whole
    words, any strides): the source's statistic form (`forms.expand`, the
    current one kept if valid) walked into the output's order and made
    row-major (`forms.gather`), moved into a window when the output is one.
    None when it is not one (or not f16)."""
    acc = plan.accesses[0]
    if acc.slot is None or plan.out_dtype != dtypes.half:
        return None
    if not dtypes.is_float(acc.dtype):
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if not live or len(live) >= WALK_LEVELS:
        return None

    st, off = FM.base_of(bufs[acc.slot])
    dt = F.np_dtype(acc.dtype)
    N = _n(st, dt)
    first = off // np.dtype(dt).itemsize + acc.off
    n = int(np.prod([U.extent(r) for r in live]))
    if first < 0:
        return None

    coef, dense = dict(acc.coef), _dense(live)
    walk = tuple((U.extent(r), coef.get(r, 0)) for r in live)
    dwalk = tuple((U.extent(r), dense[r]) for r in live)

    tmps: list = []
    try:
        if FM.ROWSTAT in st.valid and not off:
            stat = st.forms[FM.ROWSTAT].addr
        else:
            try:
                src = FM.f16(dev, st, dt, N)
            except FM.FormError:
                return None
            e = Storage(dev, FM.stat_bytes(N))
            tmps.append(e)
            FM.expand(dev, src, N, e.addr)
            stat = e.addr

        dst, doff = FM.base_of(bufs[plan.out_slot])
        oc = {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1}
        whole = not doff and (dst.nbytes == 2 * n or n % 16 == 0)
        if oc == dense and whole:
            _window_ok(dst, plan)
            FM.gather(dev, stat, N, walk, first, dwalk, n, dst.addr)
            _wrote_flat(dst, plan)
            return []

        f = Storage(dev, 2 * -(-n // 16) * 16)
        tmps.append(f)
        FM.gather(dev, stat, N, walk, first, dwalk, n, f.addr)

        key = ("gather", id(plan))
        read = Access(key, dtypes.half, tuple(dense.items()), 0)
        moved = _move(dev, replace(plan, accesses=[read]), {**bufs, key: f})
        if moved is None:
            raise Refused("a gathered read whose output window is not whole words")
        return moved
    finally:
        for t in tmps:
            t.free()


def _coalesce(plan: Plan) -> Plan:
    """Adjacent output ranges every read (and the output) walks as one run
    merged into one range: [h, w] read at strides (W*s, s) is one axis."""
    out = dict(_out_coef(plan))
    rs = [r for r in plan.out_ranges if U.extent(r) > 1]
    accs = list(plan.accesses)
    i = len(rs) - 2
    while i >= 0:
        r1, r2 = rs[i], rs[i + 1]
        e2 = U.extent(r2)
        if all(a.c(r1) == a.c(r2) * e2 for a in accs) and out[r1] == out[r2] * e2:
            r = r2.replace(src=(r2.src[0].replace(arg=U.extent(r1) * e2), *r2.src[1:]))
            accs = [
                replace(
                    a,
                    coef=tuple(
                        (r if q is r2 else q, c) for q, c in a.coef if q is not r1
                    ),
                )
                for a in accs
            ]
            out = {(r if q is r2 else q): c for q, c in out.items() if q is not r1}
            rs[i : i + 2] = [r]
        i -= 1
    if len(rs) == len([r for r in plan.out_ranges if U.extent(r) > 1]):
        return plan
    strides = tuple(out[r] for r in rs)
    dense = strides == tuple(_mult(rs))
    return replace(
        plan,
        out_ranges=tuple(rs),
        out_shape=tuple(U.extent(r) for r in rs),
        accesses=accs,
        out_strides=None if dense else strides,
    )


def _flip(dev, plan: Plan, bufs) -> list | None:
    """A read walking its innermost axis backwards (``x.flip(-1)``): the
    mover walks each row's words last to first, the vector core reverses
    each word's 16 lanes (`kernels.transpose.reverse_kernel`). None when it
    is not one."""
    acc = plan.accesses[0]
    if acc.slot is None or acc.dtype != plan.out_dtype or acc.dtype != dtypes.half:
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if not live or acc.c(live[-1]) != -1:
        return None
    C = U.extent(live[-1])
    st, off = FM.base_of(bufs[acc.slot])
    start = off + 2 * (acc.off - 15)
    others = live[:-1]
    if (
        C % 16
        or start % 32
        or start < 0
        or any(2 * acc.c(r) % 32 for r in others)
        or FLAT not in st.valid
    ):
        return None  # not whole 32-byte words: `_gather`
    levels = (*((U.extent(r), 2 * acc.c(r)) for r in others), (C // 16, -32))
    n = int(np.prod([U.extent(r) for r in live]))
    dst, doff = FM.base_of(bufs[plan.out_slot])
    oc = {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1}
    whole = oc == _dense(live) and doff == 0 and 2 * n == dst.nbytes
    t = None if whole else Storage(dev, 2 * n)
    try:
        k, given = TP.reverse_kernel(
            st.nbytes, levels, start, n, dev.machine.count("VC")
        )
        out = dst.addr if t is None else t.addr
        runs = [
            FM.launch(
                dev,
                k,
                given,
                {"x": st.addr, "y": out},
                kind="flip",
                info={"plan": plan.name},
            )
        ]
        if t is None:
            _wrote_flat(dst, plan)
            return runs
        key = ("flip", id(plan))
        read = Access(key, dtypes.half, tuple(_dense(live).items()), 0)
        moved = _move(dev, replace(plan, accesses=[read]), {**bufs, key: t})
        if moved is None:
            raise Refused("a flip whose output is not whole words")
        return runs + moved
    finally:
        if t is not None:
            t.free()


def _relayout(dev, plan: Plan, bufs) -> list | None:
    """Reads no row view takes (a transposed operand of elementwise math):
    each laid out first in the output's order (`_vector` of the bare read,
    a move or a transpose), then read dense. None when every read is fine
    or a read is not a buffer's."""
    rs = [r for r in plan.out_ranges if U.extent(r) > 1]
    if not rs or plan.reduce_ranges or _views(plan):
        return None
    cols = [rs[-1]]
    fix = [i for i, a in enumerate(plan.accesses) if classify(a, rs[:-1], cols) is None]
    if not fix or any(plan.accesses[i].slot is None for i in fix):
        return None
    runs, tmps, accesses, bufs2 = [], [], list(plan.accesses), dict(bufs)
    try:
        for i in fix:
            a = plan.accesses[i]
            key = ("lay", i, id(plan))
            own = [r for r in rs if a.c(r)]
            if (
                classify(
                    Access(key, a.dtype, tuple(_dense(own).items()), 0), rs[:-1], cols
                )
                is None
            ):
                own = rs  # broadcast across a row range no view takes: laid out in full
            n = int(np.prod([U.extent(r) for r in own])) if own else 1
            item = np.dtype(F.np_dtype(a.dtype)).itemsize
            t = Storage(dev, max(32, item * n))
            tmps.append(t)
            bufs2[key] = t
            sub = replace(
                plan,
                name=f"{plan.name}_l{i}",
                out_slot=key,
                out_dtype=a.dtype,
                out_ranges=tuple(own),
                out_shape=tuple(U.extent(r) for r in own),
                out_strides=None,
                expr=X.Load(0),
                accesses=[a],
            )
            runs += _vector(dev, sub, bufs2)
            accesses[i] = Access(key, a.dtype, tuple(_dense(own).items()), 0)
        return runs + _vector(dev, replace(plan, accesses=accesses), bufs2)
    finally:
        for t in tmps:
            t.free()


def _window_ok(dst: Storage, plan: Plan) -> None:
    """A window keeps the rest of its buffer: its flat bytes must be current."""
    if plan.out_strides is not None and FLAT not in dst.valid:
        raise Refused("a window written into a buffer whose bytes are not current")


def _wrote_flat(dst: Storage, plan: Plan) -> None:
    dst.wrote(FLAT)
    dst.dtype = F.np_dtype(plan.out_dtype)


def _out_coef(plan: Plan) -> dict:
    strides = plan.out_strides or tuple(_mult(plan.out_ranges))
    return dict(zip(plan.out_ranges, strides))


def _fill(dev, plan: Plan, bufs, value: float) -> list:
    """A box of the loop nest that is one value (a padding): the mover's
    fill over the output window."""
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    dst, doff = FM.base_of(bufs[plan.out_slot])
    dlevels = _walk(plan, _out_coef(plan), item)
    if dlevels is None or doff % 32:
        raise Refused("a padding not of whole 32-byte words")
    bits = (
        np.asarray(value, F.np_dtype(plan.out_dtype))
        .reshape(1)
        .view({1: np.uint8, 2: np.uint16, 4: np.uint32, 8: np.uint64}[item])
    )
    if item == 8:
        raise Refused("a padding of 8-byte elements")
    _window_ok(dst, plan)
    k, given = C.fill_kernel(dst.nbytes, tuple(dlevels), doff, int(bits[0]), 8 * item)
    run = FM.launch(
        dev, k, given, {"dst": dst.addr}, kind="move", info={"plan": plan.name}
    )
    _wrote_flat(dst, plan)
    return [run]


#: Most boxes a padded or selected loop nest is cut into.
MAX_BOXES = 64


def _selectors(plan: Plan) -> dict:
    """``{access index: gate?}`` of the tables a `where` selects by (a
    padded read's bound among them)."""
    out = {i: True for i, a in enumerate(plan.accesses) if a.gate}

    def walk(x):
        if isinstance(x, X.Op):
            if x.name == "where" and isinstance(x.args[0], X.Load):
                i = x.args[0].i
                if plan.accesses[i].slot is None:
                    out.setdefault(i, False)
            for a in x.args:
                walk(a)
        elif isinstance(x, X.Reduce):
            walk(x.arg)

    walk(plan.expr)
    return out


def _table_axes(a: Access) -> tuple:
    """``(ranges outermost first, the table as an array over them)``."""
    rs = [r for r, _ in sorted(a.coef, key=lambda rc: -rc[1])]
    return rs, np.asarray(a.table).reshape([U.extent(r) for r in rs])


def _boxes(dev, plan: Plan, bufs) -> list | None:
    """Tables a `where` selects by: the kept ranges cut where any changes,
    each box run with them constant -- the selects resolved, a padded read
    never reading outside its buffer. None when nothing cuts."""
    kept = [r for r in plan.out_ranges if U.extent(r) > 1]
    cuts: dict = {}
    must = False
    sel = {}
    for i, gate in _selectors(plan).items():
        rs, arr = _table_axes(plan.accesses[i])
        live = [k for k, r in enumerate(rs) if U.extent(r) > 1]
        if any(rs[k] not in kept for k in live):
            if gate:
                raise Refused(
                    "a padded read whose bound moves with a reduced range (a window: "
                    "a convolution's or a cumulative sum's)"
                )
            continue
        must |= gate
        sel[i] = gate
        for k in live:
            other = tuple(j for j in range(arr.ndim) if j != k)
            step = (
                np.any(np.diff(arr, axis=k) != 0, axis=other)
                if other
                else np.diff(arr) != 0
            )
            cuts.setdefault(rs[k], {0, U.extent(rs[k])}).update(
                int(p) + 1 for p in np.flatnonzero(step)
            )
    if not cuts:
        if not (must and kept):
            return None
        # A bound constant over the whole nest: one box resolves it.
        cuts = {kept[0]: {0, U.extent(kept[0])}}
    axes = [r for r in kept if r in cuts]
    spans = [sorted(cuts[r]) for r in axes]
    count = int(np.prod([len(s) - 1 for s in spans]))
    if count > MAX_BOXES:
        if must:
            raise Refused(f"a padded read cut into {count} boxes (at most {MAX_BOXES})")
        return None
    boxes = []
    for pick in itertools.product(*(list(itertools.pairwise(s)) for s in spans)):
        sub, sub_bufs, ok = _box(plan, dict(zip(axes, pick)), bufs, sel)
        if not ok:
            if must:
                raise Refused("a padded read whose bound is not a box of the loop nest")
            return None
        boxes.append((sub, sub_bufs))
    runs = []
    for sub, sub_bufs in boxes:
        if isinstance(sub.expr, X.Const):
            runs += _fill(dev, sub, sub_bufs, sub.expr.value)
        else:
            runs += _vector(dev, sub, sub_bufs)
    return runs


def _box(plan: Plan, box: dict, bufs, sel: dict) -> tuple:
    """``(plan, buffers, every selector constant)`` of `plan` over `box`
    (``{range: (start, end)}``): ranges narrowed, reads moved, the output a
    window of its buffer."""
    new = {
        r: r.replace(src=(r.src[0].replace(arg=e - s), *r.src[1:]))
        for r, (s, e) in box.items()
    }
    consts: dict = {}
    accesses = []
    for i, a in enumerate(plan.accesses):
        off = a.off + sum(c * box[r][0] for r, c in a.coef if r in box)
        coef = tuple((new.get(r, r), c) for r, c in a.coef)
        table = a.table
        if a.slot is None and any(r in box for r, _ in a.coef):
            rs, arr = _table_axes(a)
            sl = tuple(slice(*box[r]) if r in box else slice(None) for r in rs)
            arr = np.ascontiguousarray(arr[sl])
            keep = [new.get(r, r) for r in rs]
            coef = tuple(zip(keep, _mult(keep)))
            table, off = arr.reshape(-1), 0
            if i in sel:
                if arr.size and np.all(arr == arr.flat[0]):
                    consts[i] = float(arr.flat[0])
                else:
                    return plan, bufs, False
        accesses.append(replace(a, off=off, coef=coef, table=table))
    reductions: dict = {}

    def fold(x):
        if isinstance(x, X.Load):
            return X.Const(consts[x.i]) if x.i in consts else x
        if isinstance(x, X.Op):
            args = tuple(fold(a) for a in x.args)
            if x.name == "where" and isinstance(args[0], X.Const):
                return args[1] if args[0].value else args[2]
            return X.Op(x.name, args)
        if isinstance(x, X.Reduce):
            out = X.Reduce(x.op, fold(x.arg))
            reductions[id(out)] = tuple(new.get(r, r) for r in plan.reductions[id(x)])
            return out
        return x

    e = fold(plan.expr)
    if not isinstance(e, X.Const):
        e, accesses, remap = _prune(e, accesses)
        reductions = {k: reductions[v] for k, v in remap.items()}
    else:
        accesses = []
    mult = _out_coef(plan)
    out_ranges = tuple(new.get(r, r) for r in plan.out_ranges)
    first = sum(mult[r] * s for r, (s, _) in box.items())
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    st, off = FM.base_of(bufs[plan.out_slot])
    span = 1 + sum((U.extent(new.get(r, r)) - 1) * mult[r] for r in plan.out_ranges)
    view = StorageView(st, off + first * item, span * item)
    sub = replace(
        plan,
        out_ranges=out_ranges,
        out_shape=tuple(U.extent(r) for r in out_ranges),
        expr=e,
        accesses=accesses,
        reductions=reductions,
        reduce_ranges=tuple(new.get(r, r) for r in plan.reduce_ranges),
        out_strides=tuple(mult[r] for r in plan.out_ranges),
    )
    if tuple(mult[r] for r in plan.out_ranges) == tuple(_mult(out_ranges)):
        sub = replace(sub, out_strides=None)
    return sub, {**bufs, plan.out_slot: view}, True


def _contraction(plan: Plan) -> Plan | None:
    """`plan` as a matmul plan when it is one sum of a product of two buffer
    reads (a contraction an outer reduction left to materialise)."""
    e = plan.expr
    if not (isinstance(e, X.Reduce) and e.op == "sum" and isinstance(e.arg, X.Op)):
        return None
    if (
        e.arg.name != "mul"
        or len(e.arg.args) != 2
        or not all(isinstance(x, X.Load) for x in e.arg.args)
    ):
        return None
    reads = [plan.accesses[x.i] for x in e.arg.args if isinstance(x, X.Load)]
    if any(r.slot is None for r in reads):
        return None
    ks = tuple(r for r in plan.reduce_ranges if U.extent(r) > 1)
    if not ks:
        return None
    for a, b in (reads, reads[::-1]):
        got = L._mnk(plan, a, b, ks)
        if got is not None:
            m, n, batch = got
            return replace(
                plan,
                kind="matmul",
                expr=X.Load(0),
                accesses=[],
                a=a,
                b=b,
                mnk=(m, n, ks),
                batch=batch,
                reduce_ranges=ks,
                reductions={},
            )
    return None


def _vector(dev, plan: Plan, bufs) -> list:
    mm = _contraction(plan)
    if mm is not None:
        return _matmul(dev, mm, bufs)
    if _epilogue_contraction(plan):
        return _materialise(dev, plan, bufs)
    if _selectors(plan):
        boxed = _boxes(dev, plan, bufs)
        if boxed is not None:
            return boxed
    if len(plan.accesses) > MAX_READS:
        return _narrow(dev, plan, bufs)
    if not plan.reduce_ranges and isinstance(plan.expr, X.Load):
        one = _coalesce(plan)
        moved = (
            _move(dev, one, bufs)
            or _transpose(dev, one, bufs)
            or _flip(dev, one, bufs)
            or _gather(dev, one, bufs)
        )
        if moved is not None:
            return moved
    elif not plan.reduce_ranges:
        laid = _relayout(dev, plan, bufs)
        if laid is not None:
            return laid
    window = plan.out_strides is not None
    if window and plan.reduce_ranges:
        raise Refused("a reduction into a window of its output (a padded statistic)")
    if len(set(plan.reductions.values())) > 1:
        return _materialise(dev, plan, bufs)
    if not window and not _views(plan) and _split_reduction(plan) is None:
        runs = _segmented(dev, plan, bufs)
        if runs is not None:
            return runs
    cores = dev.machine.count("VC")
    oc = {
        r: c for r, c in zip(plan.out_ranges, _mult(plan.out_ranges)) if U.extent(r) > 1
    }
    at_out = [
        a
        for a in plan.accesses
        if _stat_only(dev, a, bufs)
        and {r: c for r, c in a.coef if U.extent(r) > 1} == oc
    ]
    if at_out and not plan.reduce_ranges:
        if window:
            raise Refused("a statistic read into a window of the output")
        # A statistic read element for element: the vector is the work.
        return _stat_space(dev, plan, bufs, cores)
    if plan.reduce_ranges:
        rows, cols = list(plan.out_ranges), _reduce_order(plan)
    else:
        flat = None if window else _flat(dev, plan, bufs, cores)
        if flat is not None:
            return flat
        plan = _refold(plan) or plan
        try:
            rows, cols = _split(plan)
        except Refused:
            narrow = None if window else _narrow_rows(dev, plan, bufs)
            if narrow is None:
                raise
            return narrow
    got = [classify(a, rows, cols) for a in plan.accesses]
    bad = [a for a, o in zip(plan.accesses, got) if o is None]
    stat_row = [
        a
        for a, o in zip(plan.accesses, got)
        if o is not None and o.kind in ("full", "col") and _stat_only(dev, a, bufs)
    ]
    if stat_row and not bad and plan.reduce_ranges and rows == list(plan.out_ranges):
        # A statistic read as rows of values along a row reduction: its
        # row-major bytes made on card (`forms.flatten`), then read as rows.
        for a in stat_row:
            st, _ = FM.base_of(bufs[a.slot])
            FM.flatten(dev, st, _n(st, F.np_dtype(a.dtype)))
        stat_row = []
    if (bad or stat_row) and plan.reduce_ranges:
        live = [r for r in rows if U.extent(r) > 1]
        partial = any(
            any(a.c(r) == 0 for r in live) and any(a.c(r) for r in live) for a in bad
        )
        if partial and not window:
            # A value broadcast over some kept ranges only: one launch an
            # index of the outer ones.
            runs = _segmented(dev, plan, bufs)
            if runs is not None:
                return runs
        # Not along the rows: down them (the kept ranges inside the reduced;
        # a statistic is a value a row, never a row of values).
        return _column(dev, plan, bufs)
    if bad:
        raise Refused(
            "an operand read as neither a whole row, one row, a value a row nor one "
            f"value (a transpose): strides {dict(bad[0].coef)}"
        )
    ops = [o for o in got if o is not None]
    R = int(np.prod([U.extent(r) for r in rows])) if rows else 1
    W = int(np.prod([U.extent(r) for r in cols])) if cols else 1
    if W % 32 and plan.reduce_ranges:
        narrow = _narrow_reduce(dev, plan, bufs, rows, cols)
        return (
            narrow if narrow is not None else _expanded_rows(dev, plan, bufs, ops, R, W)
        )
    if W % 32:
        raise Refused(f"rows of {W} with a broadcast operand: whole 32-column blocks")
    for o in ops:
        if o.kind in ("full", "col") and (
            o.offset % 16 or (o.kind == "full" and o.pitch % 16)
        ):
            raise Refused(
                f"a read not on 32-byte words (offset {o.offset}, pitch {o.pitch})"
            )
    binds, operands = {}, []
    for i, (a, o) in enumerate(zip(plan.accesses, ops)):
        at, o = _addr(dev, a, o, bufs)
        binds[f"in{i}"] = at
        operands.append(o)
    stat = bool(plan.reduce_ranges)
    if window:
        _window_ok(FM.base_of(bufs[plan.out_slot])[0], plan)
    out_at, key = _out(dev, plan, bufs, stat)
    out = (
        Operand("row")
        if stat
        else Operand("full", pitch=_out_pitch(plan, rows, cols, W))
    )
    try:
        runs = RW.run(
            dev, plan.expr, operands, binds, R, W, out, out_at, info={"plan": plan.name}
        )
    except RW.RowsError as e:
        raise Refused(str(e)) from None
    _wrote(dev, plan, bufs, key)
    return runs


def _out_access(plan: Plan) -> Access:
    return Access(plan.out_slot, plan.out_dtype, tuple(_out_coef(plan).items()), 0)


def _out_pitch(plan: Plan, rows, cols, W: int) -> int:
    """Elements between the output's rows: W, or a window's pitch."""
    if plan.out_strides is None:
        return W
    o = classify(_out_access(plan), rows, cols)
    if o is None or o.kind != "full" or o.pitch % 16:
        raise Refused("a window of the output not rows of whole 32-byte words")
    return o.pitch


def _st_dt(s, a: Access) -> tuple:
    st, _ = FM.base_of(s)
    return st, F.np_dtype(a.dtype)


def _narrow_rows(dev, plan: Plan, bufs) -> list | None:
    """Elementwise rows of W not whole 32-column blocks: computed W' =
    ceil32(W) wide (a read's extra columns are the next row's, a table
    padded with zeros), into the output's padded rows form (W' apart: read
    back compacted, read on card through `_reform`). None when the reads
    take no [rows, innermost] view or the output is not a whole f16 buffer."""
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if not live or not dtypes.is_float(plan.out_dtype):
        return None
    rows, cols = list(plan.out_ranges[:-1]), [plan.out_ranges[-1]]
    W = U.extent(cols[0])
    Wp = -(-W // 32) * 32
    dst, doff = FM.base_of(bufs[plan.out_slot])
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    if doff or item * plan.out_n != dst.nbytes:
        return None
    got = [classify(a, rows, cols) for a in plan.accesses]
    if any(o is None for o in got):
        return None
    R = int(np.prod([U.extent(r) for r in rows])) if rows else 1
    binds, operands, tmps = {}, [], []
    try:
        return _narrow_run(
            dev, plan, bufs, got, rows, cols, R, W, Wp, dst, binds, operands, tmps
        )
    finally:
        for t in tmps:
            t.free()


def _narrow_run(dev, plan, bufs, got, rows, cols, R, W, Wp, dst, binds, operands, tmps):
    if not _padded_operands(
        dev, plan, bufs, got, cols, R, W, Wp, binds, operands, tmps
    ):
        return None
    key = FM.rows_key(W, Wp)
    f = dst.form(key, 2 * R * Wp)
    try:
        runs = RW.run(
            dev,
            plan.expr,
            operands,
            binds,
            R,
            Wp,
            Operand("full", pitch=Wp),
            f.addr,
            info={"plan": plan.name, "narrow": W},
        )
    except RW.RowsError as e:
        raise Refused(str(e)) from None
    dst.wrote(key)
    dst.dtype = F.np_dtype(plan.out_dtype)
    return runs


#: What a padding lane holds so a reduction ignores it.
IDENTITY = {"sum": 0.0, "max": -65504.0, "prod": 1.0}


def _narrow_reduce(dev, plan: Plan, bufs, rows, cols) -> list | None:
    """A reduction along rows of W not whole 32-column blocks: run W' =
    ceil32(W) wide over the padded reads (`_padded_operands`), each
    reduction's padding lanes its identity by a column mask. None when a
    read does not allow it (the statistic-space path is then taken)."""
    live = [r for r in cols if U.extent(r) > 1]
    if len(live) != 1:
        return None
    col = live[0]
    W = U.extent(col)
    Wp = -(-W // 32) * 32
    got = [classify(a, rows, cols) for a in plan.accesses]
    if any(o is None for o in got):
        return None
    R = int(np.prod([U.extent(r) for r in rows])) if rows else 1
    mask = Access(
        None, dtypes.half, ((col, 1),), 0, (np.arange(Wp) < W).astype(np.float16)
    )
    k = len(plan.accesses)

    def guard(x):
        if isinstance(x, X.Reduce):
            return X.Reduce(
                x.op, X.op("where", X.Load(k), guard(x.arg), IDENTITY[x.op])
            )
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(guard(a) for a in x.args))
        return x

    padded = replace(plan, expr=guard(plan.expr), accesses=[*plan.accesses, mask])
    got = [*got, Operand("col")]
    binds, operands, tmps = {}, [], []
    try:
        if not _padded_operands(
            dev,
            padded,
            bufs,
            got,
            [col],
            R,
            W,
            Wp,
            binds,
            operands,
            tmps,
            masked=frozenset({k}),
        ):
            return None
        out_at, key = _out(dev, plan, bufs, True)
        try:
            runs = RW.run(
                dev,
                padded.expr,
                operands,
                binds,
                R,
                Wp,
                Operand("row"),
                out_at,
                info={"plan": plan.name, "narrow": W},
            )
        except RW.RowsError as e:
            raise Refused(str(e)) from None
        _wrote(dev, plan, bufs, key)
        return runs
    finally:
        for t in tmps:
            t.free()


def _padded_operands(
    dev, plan, bufs, got, cols, R, W, Wp, binds, operands, tmps, masked=frozenset()
) -> bool:
    """Each read in the form a W'-wide rows kernel takes: rows closer than
    W' laid out W' apart by the mover (whole words), a table over the
    columns padded with zeros (`masked` ones are already W' long). False
    when a read cannot be."""
    for i, (a, o) in enumerate(zip(plan.accesses, got)):
        if i in masked:
            binds[f"in{i}"] = FM.table(dev, a.table)
            operands.append(o)
            continue
        assert o is not None
        if a.slot is not None and o.kind == "full" and o.pitch < Wp:
            # Rows read W' wide: laid out W' apart first (whole words only).
            if (2 * W) % 32 or o.offset % 16 or o.pitch % 16:
                return False
            src = FM.f16(
                dev, bufs[a.slot], F.np_dtype(a.dtype), _n(*_st_dt(bufs[a.slot], a))
            )
            t = Storage(dev, 2 * R * Wp)
            tmps.append(t)
            FM.repitch(dev, src + 2 * o.offset, o.pitch, t.addr, Wp, R, W)
            binds[f"in{i}"] = t.addr
            operands.append(Operand("full", pitch=Wp))
            continue
        if a.slot is None and a.c(cols[0]):
            rs, arr = _table_axes(a)
            arr = np.pad(arr, [(0, Wp - W if r is cols[0] else 0) for r in rs])
            ext = [Wp if r is cols[0] else U.extent(r) for r in rs]
            co, step = {}, 1
            for r, e in reversed(list(zip(rs, ext))):
                co[r] = step
                step *= e
            a = replace(a, table=arr.reshape(-1), coef=tuple(co.items()))
            if o.kind == "full":
                o = Operand("full", pitch=Wp, offset=o.offset // W * Wp)
        if o.kind in ("full", "col") and (
            o.offset % 16 or (o.kind == "full" and o.pitch % 16)
        ):
            return False
        at, o = _addr(dev, a, o, bufs)
        binds[f"in{i}"] = at
        operands.append(o)
    return True


def _expanded_rows(dev, plan: Plan, bufs, ops, R: int, W: int) -> list:
    """A reduction along rows not of whole 32-column blocks: each operand's
    [n, 32] form is the operand with every value repeated 32 times, so a row
    is 32 * W columns; a sum of it is 32 times the row's."""
    if any(r.op == "prod" for r in _all_reduces(plan.expr)):
        raise Refused(f"a product along rows of {W}: whole 32-column blocks")
    binds, operands = {}, []
    for i, (a, o) in enumerate(zip(plan.accesses, ops)):
        if a.slot is None:
            binds[f"in{i}"] = _table_rowstat(dev, a)
        else:
            st, off = FM.base_of(bufs[a.slot])
            if off:
                raise Refused("a statistic of a sub-buffer")
            dt = F.np_dtype(a.dtype)
            binds[f"in{i}"] = FM.rowstat(dev, st, dt, _n(st, dt))
        if o.kind == "full":
            o = Operand("full", pitch=o.pitch * 32, offset=o.offset * 32, rows=R)
        elif o.kind == "col":
            o = Operand("col", offset=o.offset * 32)
        operands.append(o)

    def scale(x):
        if isinstance(x, X.Reduce):
            inner = X.Reduce(x.op, scale(x.arg))
            return X.op("mul", inner, 1.0 / 32) if x.op == "sum" else inner
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(scale(a) for a in x.args))
        return x

    out_at, key = _out(dev, plan, bufs, True)
    try:
        runs = RW.run(
            dev,
            scale(plan.expr),
            operands,
            binds,
            R,
            32 * W,
            Operand("row"),
            out_at,
            info={"plan": plan.name, "expanded": W},
        )
    except RW.RowsError as e:
        raise Refused(str(e)) from None
    except OutOfMemory as e:
        raise Refused(f"rows of {W} read as {32 * W} statistic columns: {e}") from None
    _wrote(dev, plan, bufs, key)
    return runs


def _reduce_order(plan) -> list:
    """The reduced ranges, outermost first by an operand reading them all."""
    rs = list(plan.reduce_ranges)
    for a in plan.accesses:
        if all(a.c(r) for r in _live(rs)):
            return sorted(rs, key=lambda r: -a.c(r))
    return rs


#: Row widths a lone long range is folded into, widest first.
FOLD_WIDTHS = (256, 128, 64, 32)


def _refold(plan: Plan) -> Plan | None:
    """An elementwise nest that is one range longer than a row block (a
    flat update read beside scalars): the range viewed as [n / c, c] rows,
    every stride s on it split into (s * c, s). None when it is not one."""
    if plan.out_strides is None:
        plan = _coalesce(plan)
    live = _live(plan.out_ranges)
    if len(live) != 1 or plan.reduce_ranges or plan.out_strides is not None:
        return None
    r = live[0]
    n = U.extent(r)
    c = next((w for w in FOLD_WIDTHS if n % w == 0 and n // w > 1), None)
    if n <= V.MAX_BLOCK or c is None:
        return None

    used = [q for a in plan.accesses for q, _ in a.coef] + list(plan.out_ranges)
    nxt = 1 + max(q.arg[0] for q in used)
    rr = UOp.range(n // c, nxt, r.arg[1])
    cc = UOp.range(c, nxt + 1, r.arg[1])

    def split(coef) -> tuple:
        out = []
        for q, s in coef:
            out += [(rr, s * c), (cc, s)] if q is r else [(q, s)]
        return tuple(out)

    out_ranges = tuple(
        x for q in plan.out_ranges for x in ((rr, cc) if q is r else (q,))
    )
    return replace(
        plan,
        out_ranges=out_ranges,
        out_shape=tuple(U.extent(q) for q in out_ranges),
        accesses=[replace(a, coef=split(a.coef)) for a in plan.accesses],
    )


def _split(plan: Plan) -> tuple:
    """[rows, cols] of an elementwise kernel: the widest whole-32 column
    set every read classifies against."""
    order = list(plan.out_ranges)
    accs = [
        *plan.accesses,
        *([_out_access(plan)] if plan.out_strides is not None else []),
    ]
    for s in range(len(order) - 1, -1, -1):
        rows, cols = order[:s], order[s:]
        w = int(np.prod([U.extent(r) for r in cols])) if cols else 1
        if w % 32:
            continue
        if all(classify(a, rows, cols) is not None for a in accs):
            return rows, cols
    raise Refused(
        "no rows-and-columns view of the loop nest reads every operand as whole "
        "32-column rows, one row, a value a row or one value"
    )


def _flat(dev, plan: Plan, bufs, cores):
    """Every read at the output's own index: N elements as 128-value rows."""
    oc = dict(zip(plan.out_ranges, _mult(plan.out_ranges)))
    oc = {r: c for r, c in oc.items() if U.extent(r) > 1}
    for a in plan.accesses:
        coef = {r: c for r, c in a.coef if U.extent(r) > 1}
        if coef != oc or a.off % 16:
            return None
    n = plan.out_n
    binds, dts = {}, []
    for i, a in enumerate(plan.accesses):
        if a.slot is None:
            binds[f"in{i}"] = FM.table(dev, a.table)
        else:
            s = bufs[a.slot]
            dt = F.np_dtype(a.dtype)
            st, _ = FM.base_of(s)
            binds[f"in{i}"] = FM.f16(dev, s, dt, _n(st, dt)) + a.off * 2
        dts.append("f16")
    out_at, key = _out(dev, plan, bufs, False)
    k, given = V.flat_kernel(plan.expr, tuple(dts), n, "f16", cores)
    binds["out"] = out_at
    run = FM.launch(dev, k, given, binds, info={"plan": plan.name})
    _wrote(dev, plan, bufs, key)
    return [run]


def _stat_space(dev, plan: Plan, bufs, cores) -> list:
    """Operands that are statistics: the expression over their [n, 32] forms
    (each element of the vector a row), the output a statistic too."""
    oc = {
        r: c for r, c in zip(plan.out_ranges, _mult(plan.out_ranges)) if U.extent(r) > 1
    }
    binds, operands = {}, []
    n = plan.out_n
    for i, a in enumerate(plan.accesses):
        coef = {r: c for r, c in a.coef if U.extent(r) > 1}
        if a.slot is None:
            binds[f"in{i}"] = _table_rowstat(dev, a)
            if coef != oc:
                raise Refused("a folded table read at another index than a statistic")
            operands.append(Operand("full", pitch=32, rows=n))
            continue
        st, _ = FM.base_of(bufs[a.slot])
        dt = F.np_dtype(a.dtype)
        if not coef:
            binds[f"in{i}"] = FM.rowstat(dev, st, dt, _n(st, dt))
            operands.append(Operand("scalar", offset=a.off))
        elif coef == oc:
            binds[f"in{i}"] = FM.rowstat(dev, st, dt, _n(st, dt))
            operands.append(Operand("full", pitch=32, offset=a.off * 32, rows=n))
        else:
            raise Refused("a statistic read at another index than the output's")
    out_at, key = _out(dev, plan, bufs, True)
    k, given, _ = V.rows_kernel(
        plan.expr, tuple(operands), n, 32, Operand("full", pitch=32), cores
    )
    binds["out"] = out_at
    run = FM.launch(dev, k, given, binds, info={"plan": plan.name, "space": "stat"})
    _wrote(dev, plan, bufs, key)
    return [run]


# --------------------------------------------------------------- matmul
def _ks(k) -> tuple:
    """A matmul's K: one range or several (laid out as one)."""
    return tuple(k) if isinstance(k, tuple) else (k,)


def _matmul(dev, plan: Plan, bufs) -> list:
    m, n, k = plan.mnk
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    if not live or live[-1] is not n:
        raise Refused("a product stored transposed")
    a, b = plan.a, plan.b
    if a is None or b is None:
        raise Refused("a matmul plan without its A and B reads")
    batch = [r for r in plan.out_ranges if r in plan.batch]
    # An operand not [.., rows, K] row-major (x @ w with w [K, N], x.T @ y):
    # laid out first, on card, as [batch it reads.., rows, K].
    runs, tmps, bufs = [], [], dict(bufs)
    try:
        for side, acc, row in (("a", a, m), ("b", b, n)):
            order = [*(r for r in batch if acc.c(r)), row, *_ks(k)]
            if {r: c for r, c in acc.coef if U.extent(r) > 1} == {
                r: c for r, c in _dense(order).items() if U.extent(r) > 1
            }:
                continue
            n_el = int(np.prod([U.extent(r) for r in order]))
            t = Storage(dev, max(32, 2 * n_el))
            tmps.append(t)
            key = ("mm", side, id(plan))
            bufs[key] = t
            sub = replace(
                plan,
                kind="vector",
                name=f"{plan.name}_{side}",
                out_slot=key,
                # The product quantises f16: the layout is f16 whatever acc's type.
                out_dtype=dtypes.half,
                out_ranges=tuple(order),
                out_shape=tuple(U.extent(r) for r in order),
                out_strides=None,
                expr=X.Load(0),
                accesses=[acc],
                reduce_ranges=(),
                reductions={},
                a=None,
                b=None,
                mnk=(),
                batch=(),
            )
            runs += _vector(dev, sub, bufs)
            new = Access(key, dtypes.half, tuple(_dense(order).items()), 0)
            if side == "a":
                a = new
            else:
                b = new
        return runs + _product(dev, replace(plan, a=a, b=b), bufs)
    finally:
        for t in tmps:
            t.free()


def _product(dev, plan: Plan, bufs) -> list:
    m, n, k = plan.mnk
    a, b = plan.a, plan.b
    assert a is not None and b is not None
    M, N = U.extent(m), U.extent(n)
    K = int(np.prod([U.extent(r) for r in _ks(k)]))
    batch = [r for r in plan.out_ranges if r in plan.batch]
    bt = int(np.prod([U.extent(r) for r in batch])) if batch else 1
    pa, pb = a.c(m), b.c(n)
    _batched(a, batch, M * pa, "A")
    shared = not any(b.c(r) for r in batch)
    if not shared:
        _batched(b, batch, N * pb, "B")
    # N in whole 32-column blocks: B's padding rows quantise to zero, the
    # product goes to an [rows, Np] temporary and its window is moved out.
    Np = -(-N // 32) * 32
    try:
        mm = MM.plan(bt, M, Np, K, shared)
    except ValueError as e:
        raise Refused(str(e)) from None
    da, db = F.np_dtype(a.dtype), F.np_dtype(b.dtype)
    qa = FM.mx7(
        dev,
        bufs[a.slot],
        da,
        Operand("full", pitch=pa, offset=a.off),
        M,
        K,
        mm,
        "a",
        bt,
    )
    qb = FM.mx7(
        dev,
        bufs[b.slot],
        db,
        Operand("full", pitch=pb, offset=b.off),
        N,
        K,
        mm,
        "b",
        1 if shared else bt,
    )
    rows = list(plan.out_ranges[:-1])
    extras, binds = [], {"a": qa, "b": qb}
    lays: list = []  # epilogue rows laid out Np apart, freed after the run
    for i, acc in enumerate(plan.accesses, start=1):
        o = classify(acc, rows, [n])
        if o is None and acc.slot is not None:
            # Broadcast over some rows only (a position table over a batch):
            # laid out over the output's ranges first (a move, stride 0).
            t = Storage(dev, max(32, 2 * plan.out_n))
            lays.append(t)
            key = ("epi", i, id(plan))
            sub = replace(
                plan,
                kind="vector",
                name=f"{plan.name}_e{i}",
                out_slot=key,
                out_dtype=dtypes.half,
                out_strides=None,
                expr=X.Load(0),
                accesses=[acc],
                reduce_ranges=(),
                reductions={},
                a=None,
                b=None,
                mnk=(),
                batch=(),
            )
            bufs = {**bufs, key: t}
            _vector(dev, sub, bufs)
            acc = Access(
                key, dtypes.half, tuple(_dense(list(plan.out_ranges)).items()), 0
            )
            o = classify(acc, rows, [n])
        if o is None:
            for t in lays:
                t.free()
            raise Refused(
                f"an epilogue operand read as no row form: strides {dict(acc.coef)}"
            )
        at, o = _addr(dev, acc, o, bufs)
        if o.kind == "full" and o.pitch < Np:
            # Rows read Np wide: laid out Np apart first (whole words only).
            if (2 * N) % 32 or o.offset % 16 or o.pitch % 16:
                for t in lays:
                    t.free()
                raise Refused(
                    f"an epilogue operand of rows of {N}: not whole 32-byte words"
                )
            t = Storage(dev, 2 * bt * M * Np)
            lays.append(t)
            FM.repitch(dev, at + 2 * o.offset, o.pitch, t.addr, Np, bt * M, N)
            at, o = t.addr, Operand("full", pitch=Np, rows=bt * M)
        binds[f"in{i}"] = at
        extras.append(o)
    try:
        return _product_run(
            dev, plan, bufs, mm, binds, extras, bt, M, N, Np, batch, m, n
        )
    finally:
        for t in lays:
            t.free()


def _product_run(dev, plan, bufs, mm, binds, extras, bt, M, N, Np, batch, m, n) -> list:
    padded = Np != N
    tmp: Storage | None = None
    dst, doff = FM.base_of(bufs[plan.out_slot])
    # Rows of N not whole words: the product stays in the output's padded
    # rows form (read back compacted; readers on card take its strides).
    whole = not doff and plan.out_strides is None
    narrow = (2 * N) % 32 != 0 or plan.out_dtype != dtypes.half
    in_form = padded and narrow and whole
    if padded:
        if not dtypes.is_float(plan.out_dtype) or (narrow and not whole):
            raise Refused(
                f"a {plan.out_dtype} product of {N} columns (not whole blocks of 32) "
                "into a window of its buffer"
            )
        if in_form:
            out_at, key = dst.form(FM.rows_key(N, Np), 2 * bt * M * Np).addr, None
        else:
            tmp = Storage(dev, 2 * bt * M * Np)
            out_at, key = tmp.addr, None
    else:
        out_at, key = _out(dev, plan, bufs, False)
    binds["out"] = out_at
    try:
        kern, given = MM.mm_kernel(
            mm,
            plan.expr,
            tuple(extras),
            Operand("full", pitch=Np, rows=bt * M),
            dev.machine.count("MG"),
            dev.machine.count("VC"),
        )
        run = FM.launch(
            dev, kern, given, binds, kind="matmul", info={"plan": plan.name, "mm": mm}
        )
        if not padded:
            _wrote(dev, plan, bufs, key)
            return [run]
        if in_form:
            dst.wrote(FM.rows_key(N, Np))
            dst.dtype = F.np_dtype(plan.out_dtype)
            return [run]
        # The [.., M, N] window of the [.., M, Np] product.
        tkey = ("mmpad", id(plan))
        coef, step = {}, 1
        for r, e in reversed(
            list(zip([*batch, m, n], [*(U.extent(x) for x in batch), M, Np]))
        ):
            coef[r] = step
            step *= e
        sub = replace(
            plan,
            kind="vector",
            expr=X.Load(0),
            accesses=[Access(tkey, dtypes.half, tuple(coef.items()), 0)],
            reduce_ranges=(),
            reductions={},
        )
        moved = _move(dev, sub, {**bufs, tkey: tmp})
        if moved is None:
            raise Refused(
                f"a product of {N} columns: its window is not whole 32-byte words"
            )
        return [run, *moved]
    finally:
        if tmp is not None:
            tmp.free()


def _batched(acc, batch, step: int, what: str) -> None:
    """Refuse a batched operand whose batches are not one row-major stack."""
    want = step
    for r in reversed(batch):
        if U.extent(r) > 1 and acc.c(r) != want:
            raise Refused(
                f"{what}'s batches are not one row-major stack of [rows, K] blocks"
            )
        want *= U.extent(r)


__all__ = ["classify", "execute"]
