"""One kernel per plan class, each classifying its reads and launching."""

import itertools
from dataclasses import replace
from typing import Any

import numpy as np
from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.launch.config import _active_scope, elide_rule
from kohakutpu.application.ktpugrad.planning import fold as F
from kohakutpu.application.ktpugrad.planning import window as W
from kohakutpu.application.ktpugrad.planning.lower import Access, Plan, Refused
from kohakutpu.application.ktpugrad.run.common import (
    _addr,
    _dense,
    _half_target,
    _held,
    _hold,
    _mult,
    _n,
    _out,
    _out_coef,
    _read_id,
    _reform,
    _size,
    _walk,
    _wrote,
)
from kohakutpu.application.ktpugrad.run.moves import _bare, _fill, _gather_to, _moved
from kohakutpu.application.ktpugrad.run.pad import (
    _lay,
    _pad_copy,
    _window_lead,
    _window_swapped,
)
from kohakutpu.application.ktpugrad.run.product import (
    _batched,
    _box,
    _ks,
    _packing_plan,
    _pending,
    _product_run,
    _rows_form,
)
from kohakutpu.application.ktpugrad.run.reduce import (
    IDENTITY,
    MAX_BOXES,
    MAX_READS,
    _all_reduces,
    _leaf,
    _leaf_reduce,
    _loads,
    _prune,
    _rebuild,
    _refold,
    _selectors,
    _split_reduction,
    _table_axes,
    _vector_choice,
)
from kohakutpu.application.ktpugrad.run.shape import (
    _bind,
    _laid,
    _laid_plan,
    _place,
    _reduce_order,
    _table_over,
    _takes,
    _view,
    _wide,
    _zero_lanes,
    classify,
)
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import tree as TR
from kohakutpu.application.runtime.memory.storage import FLAT, Storage, StorageView
from kohakutpu.language.numerics import fp16


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

    def swap(x, walk):
        if x is leaf:
            return X.Load(len(plan.accesses))
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, walk(x.arg))
            reductions[id(new)] = plan.reductions[id(x)]
            return new
        return None

    outer_e = _rebuild(plan.expr, swap)
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

    def swap(x, walk):
        if x is best[1]:
            return X.Load(len(plan.accesses))
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, walk(x.arg))
            reductions[id(new)] = plan.reductions[id(x)]
            return new
        return None

    outer_e, outer_acc, remap = _prune(
        _rebuild(plan.expr, swap), [*plan.accesses, read]
    )
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


def _boxes(dev, plan: Plan, bufs) -> list | None:
    """Tables a `where` selects by: the kept ranges cut where any changes,
    each box run with them constant -- the selects resolved, a padded read
    never reading outside its buffer. None when nothing cuts, a bound moves
    with a reduced range, the cuts are not boxes or more than `MAX_BOXES`,
    or a box's output is not a window of whole words (`_ungated` then reads
    the padded reads in bounds)."""
    kept = [r for r in plan.out_ranges if U.extent(r) > 1]
    cuts: dict = {}
    must = False
    sel = {}
    for i, gate in _selectors(plan).items():
        rs, arr = _table_axes(plan.accesses[i])
        live = [k for k, r in enumerate(rs) if U.extent(r) > 1]
        if any(rs[k] not in kept for k in live):
            if gate:
                return None
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
        return None
    boxes = []
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    for pick in itertools.product(*(list(itertools.pairwise(s)) for s in spans)):
        sub, sub_bufs, ok = _box(plan, dict(zip(axes, pick)), bufs, sel)
        doff = FM.base_of(sub_bufs[sub.out_slot])[1]
        if not ok or doff % 32 or _walk(sub, _out_coef(sub), item) is None:
            return None
        boxes.append((sub, sub_bufs))
    runs = []
    for sub, sub_bufs in boxes:
        if isinstance(sub.expr, X.Const):
            runs += _fill(dev, sub, sub_bufs, sub.expr.value)
        else:
            runs += _vector(dev, sub, sub_bufs)
    return runs


def _ungated(dev, plan: Plan, bufs) -> list:
    """Each padded read (a buffer read a gate table selects) read from a
    zero-padded copy of its source over its coordinates instead
    (`_pad_copy`; the gates a box of them, `window.boxed`): no read leaves
    its buffer, the gates stay selects."""
    guards: dict = {}  # read -> the gates selecting it

    def walk(x, gates):
        if isinstance(x, X.Load):
            if plan.accesses[x.i].slot is not None and gates:
                guards.setdefault(x.i, set()).update(gates)
        elif isinstance(x, X.Op):
            g = x.args[0] if x.name == "where" else None
            if isinstance(g, X.Load) and plan.accesses[g.i].gate:
                walk(x.args[1], gates | {g.i})
                walk(x.args[2], gates)
                return
            for a in x.args:
                walk(a, gates)
        elif isinstance(x, X.Reduce):
            walk(x.arg, gates)

    walk(plan.expr, frozenset())
    bufs, tmps, accesses = dict(bufs), [], list(plan.accesses)
    try:
        for i, gs in guards.items():
            acc = accesses[i]
            rs, g = W.gate_of(plan, sorted(gs))
            # Where the read is: the gate over the ranges it reads.
            mine = {r for r, c in acc.coef if c}
            other = tuple(k for k, r in enumerate(rs) if r not in mine)
            g = g.any(axis=other) if other else g
            rs = [r for r in rs if r in mine]
            side = W.Side(acc, W.dims(acc, W.coupled(rs, g)), None)
            # The gate stays a select: the copy need only hold every
            # coordinate it lets through.
            if not g.any():
                # Never let through: a zero (the select takes the other arm).
                zero = np.zeros(1, np.float16)
                accesses[i] = Access(None, dtypes.half, (), 0, zero)
                continue
            side = W.boxed(side, rs, g, exact=False)
            if side is None:
                raise Refused("a padded read whose gate reads a range it does not")
            key, strides, off, _ = _pad_copy(
                dev, bufs, tmps, acc, list(side.dims), ("ungated", i, id(plan))
            )
            coef = tuple((r, m * strides[d]) for d in side.dims for r, m in d.terms)
            accesses[i] = Access(key, dtypes.half, coef, off)
        accesses = [replace(a, gate=False) for a in accesses]
        return _vector(dev, replace(plan, accesses=accesses), bufs)
    finally:
        for t in tmps:
            t.free()


def _window(dev, plan: Plan, bufs, contraction=None) -> list | None:
    """A window contraction (`window.find`): each side laid out dense as
    ``[batch.., outer.., K.., lane]`` (`_lay`), the product on the clusters
    into a temporary, the rest of the expression reading it. None when the
    plan is not one."""
    c = W.find(plan) if contraction is None else contraction
    if c is None:
        return None
    plan = c.plan
    # The product in the output's order (`_window_swapped`): its rows the B
    # side's, A's outer ranges before every B one in the output a batch (B
    # read the same in each), so the expression around it reads it in place.
    swapped = _window_swapped(plan, c)
    lead = _window_lead(plan, c) if swapped else []
    bt = (*c.bt, *lead)
    ma = tuple(r for r in c.ma if r not in lead)
    M, N, K = _size(ma), _size(c.nb), _size(c.ks) * c.pad
    m, n, k, lane = W.ranges(plan, [M, N, K, c.pad])
    bufs, tmps, runs = dict(bufs), [], []
    try:
        keys = []
        for side, outer in ((c.a, ma), (c.b, c.nb)):
            key = ("win", len(keys), id(plan))
            runs += _lay(dev, plan, bufs, tmps, c, side, [*bt, *outer], lane, key)
            keys.append(key)
        a, b = (
            Access(
                key,
                dtypes.half,
                (*zip(bt, (s * R * K for s in _mult(bt))), (r, K), (k, 1)),
                0,
            )
            for key, r, R in ((keys[0], m, M), (keys[1], n, N))
        )
        rows_side, cols_side = (c.nb, ma) if swapped else (ma, c.nb)
        if swapped:
            a, b, m, n = b, a, n, m
        out = (*bt, m, n)
        order = [*bt, *rows_side, *cols_side]
        live = [r for r in plan.out_ranges if U.extent(r) > 1]
        direct = (
            plan.expr is c.red
            and c.scale == 1.0
            and plan.out_strides is None
            and live == [r for r in order if U.extent(r) > 1]
        )
        if direct:
            # The output is the product, in its order: written there.
            pkey, pdtype = plan.out_slot, plan.out_dtype
        else:
            pkey, pdtype = ("winp", id(plan)), dtypes.half
            tp = Storage(dev, max(32, 2 * _size(bt) * M * N))
            tmps.append(tp)
            bufs[pkey] = tp
        runs += _product(
            dev,
            replace(
                plan,
                kind="matmul",
                name=f"{plan.name}_mm",
                out_slot=pkey,
                out_dtype=pdtype,
                out_ranges=out,
                out_shape=tuple(U.extent(r) for r in out),
                out_strides=None,
                expr=X.Load(0),
                accesses=[],
                reduce_ranges=(k,),
                reductions={},
                a=a,
                b=b,
                mnk=(m, n, (k,)),
                batch=bt,
            ),
            bufs,
        )
        if direct:
            return runs
        # The rest of the expression, the sum a read of the product.
        read = Access(pkey, dtypes.half, tuple(_dense(order).items()), 0)
        prod = X.Load(len(plan.accesses))
        if c.scale != 1.0:
            prod = X.op("mul", X.Const(c.scale), prod)

        def swap(x):
            if x is c.red:
                return prod
            if isinstance(x, X.Op):
                return X.Op(x.name, tuple(swap(y) for y in x.args))
            return x

        e, accs, _ = _prune(swap(plan.expr), [*plan.accesses, read])
        epi = replace(plan, expr=e, accesses=accs, reduce_ranges=(), reductions={})
        epi, bufs = _reform(dev, epi, bufs)
        return runs + _vector(dev, epi, bufs)
    finally:
        for t in tmps:
            t.free()


def _vector(dev, plan: Plan, bufs) -> list:
    payload: Any
    choice, payload = _vector_choice(plan)
    if choice == "window":
        return _window(dev, plan, bufs, payload)
    if choice == "tabled":
        return _uploaded(dev, payload, bufs)
    if choice == "enumerated":
        return _enumerated(dev, plan, bufs, payload)
    if choice == "matmul":
        return _matmul(dev, payload, bufs)
    if choice == "materialise":
        return _materialise(dev, plan, bufs)
    if _selectors(plan):
        boxed = _boxes(dev, plan, bufs)
        if boxed is not None:
            return boxed
        if any(a.gate for a in plan.accesses):
            return _ungated(dev, plan, bufs)
    if len(plan.accesses) > MAX_READS:
        return _narrow(dev, plan, bufs)
    if not plan.reduce_ranges and isinstance(plan.expr, X.Load):
        moved = _bare(dev, plan, bufs)
        if moved is not None:
            return moved
    if len(set(plan.reductions.values())) > 1:
        return _materialise(dev, plan, bufs)
    region = bool(_all_reduces(plan.expr)) and not plan.reduce_ranges
    if not region and not plan.reduce_ranges and plan.out_strides is None:
        cores = dev.machine.count("VC")
        flat = _flat(dev, plan, bufs, cores)
        if flat is not None:
            return flat
    if not region and not plan.reduce_ranges:
        plan = _refold(plan) or plan
    return _rows(dev, plan, bufs)


def _rows(dev, plan: Plan, bufs) -> list:
    """`plan` -- one reduction, or none -- on the rows kernels: its view
    (`_view`), each read bound to it (`_bind`), W' = ceil32(W) columns wide
    (a reduction's padding lanes its identity), the result placed
    (`_place`). A reduction whose view takes a read only laid out, and whose
    ranges interleave with the kept ones, runs as two (`_split_reduction`);
    reductions down the rows (a tree holds one) one after another."""
    kind, rows, cols = _view(plan)
    Wp = _wide(cols)
    if plan.reduce_ranges and _laid(plan, rows, cols, Wp, out=False):
        split = _split_reduction(plan)
        if split is not None:
            return _materialise(dev, split, bufs)
    if kind == "down" and len(_all_reduces(plan.expr)) > 1:
        return _materialise(dev, plan, bufs)
    W, R = _size(cols), _size(rows)
    expr = plan.expr
    binds, operands, tmps, runs = {}, [], [], []
    try:
        for i, a in enumerate(plan.accesses):
            at, o, got = _bind(dev, plan, a, rows, cols, Wp, bufs, tmps)
            binds[f"in{i}"] = at
            operands.append(o)
            runs += got
        if kind != "down" and RW._reductions(plan.expr) and Wp != W:
            k = len(operands)
            binds[f"in{k}"] = FM.table(dev, (np.arange(Wp) < W).astype(np.float16))
            operands.append(Operand("col"))

            def guard(x, walk):
                if isinstance(x, X.Reduce):
                    lane = X.op("where", X.Load(k), walk(x.arg), IDENTITY[x.op])
                    return X.Reduce(x.op, lane)
                return None

            expr = _rebuild(expr, guard)
        site = ("rows", repr(plan.expr), R, W)
        zero = kind == "rows" and Wp != W and FM.hints(dev).get(site) == "rows"
        if zero:
            # Its consumers read its padded rows whole (`forms.hints`): the
            # padding lanes stored as zeros.
            k = len(operands)
            binds[f"in{k}"] = FM.table(dev, (np.arange(Wp) < W).astype(np.float16))
            operands.append(Operand("col"))
            expr = _zero_lanes(expr, k)
        out_at, out, done = _place(dev, plan, bufs, kind, rows, cols, Wp, tmps)
        kernel = TR.column if kind == "down" else RW.run
        info = {"plan": plan.name, "view": kind}
        scope = _active_scope(dev)
        if (
            scope is not None
            and kind == "rows"
            and not RW._reductions(expr)
            and out.kind != "row"
            # A `forms.Pending` read is made inside its own rows kernel
            # (`rows.run`); a multi-stage package takes only addresses.
            and not any(isinstance(v, FM.Pending) for v in binds.values())
        ):
            RW._whole_rows(operands, Wp)
            ops_b, binds_b = RW._based(operands, dict(binds, out=out_at), "in")
            (out_b,), binds_b = RW._based([out], binds_b, "out")
            scope.stages.append(
                ("rows", expr, tuple(ops_b), R, Wp, out_b, dict(binds_b), info)
            )
            runs += done()
            dst = FM.base_of(bufs[plan.out_slot])[0]
            key = FM.padded(dst)
            if key == FM.rows_key(W, Wp):
                dst.site = (site, W)
                if zero:
                    dst.zeroed.add(key)
            scope.tmps.extend(tmps)
            tmps = []
            return runs
        runs += kernel(dev, expr, operands, binds, R, Wp, out, out_at, info)
        runs += done()
        if kind == "rows":
            dst = FM.base_of(bufs[plan.out_slot])[0]
            key = FM.padded(dst)
            if key == FM.rows_key(W, Wp):
                dst.site = (site, W)
                if zero:
                    dst.zeroed.add(key)
    except RW.RowsError as e:
        raise Refused(str(e)) from None
    finally:
        scope = _active_scope(dev)
        for t in tmps:
            if scope is not None:
                held = [k for k, v in scope.laid_cache.items() if v is t]
                for k in held:
                    del scope.laid_cache[k]
            t.free()
    return runs


def _uploaded(dev, plan: Plan, bufs) -> list:
    """`plan` with each folded table its reductions read uploaded as an f16
    buffer (an operand the clusters can take)."""
    under: set = set()

    def walk(x, inside):
        if isinstance(x, X.Load) and inside:
            under.add(x.i)
        elif isinstance(x, X.Op):
            for a in x.args:
                walk(a, inside)
        elif isinstance(x, X.Reduce):
            walk(x.arg, True)

    walk(plan.expr, False)
    bufs, tmps, accesses = dict(bufs), [], list(plan.accesses)
    try:
        for i in sorted(under):
            a = accesses[i]
            if a.slot is not None:
                continue
            raw = np.ascontiguousarray(a.table, np.float16).reshape(-1)
            t = Storage(dev, max(32, raw.nbytes))
            tmps.append(t)
            dev.write(t.addr, raw.tobytes())
            t.dtype = np.float16
            key = ("table", i, id(plan))
            bufs[key] = t
            accesses[i] = replace(a, slot=key, dtype=dtypes.half, table=None)
        return _vector(dev, replace(plan, accesses=accesses), bufs)
    finally:
        for t in tmps:
            t.free()


def _enumerated(dev, plan: Plan, bufs, phases: list) -> list:
    """`plan` once a value of the kept ranges `phases` (each table then
    reads the reduced ranges alone: a window contraction), each into its
    slab of an f16 temporary ``[phases.., the other kept ranges..]`` (slabs
    whole words), then the temporary moved into the output."""
    rest = [r for r in plan.out_ranges if r not in phases]
    slab = -(-_size(rest) // 16) * 16
    strides = [*(s * slab for s in _mult(phases)), *_mult(rest)]
    order = [*phases, *rest]
    key = ("phase", id(plan))
    t = Storage(dev, max(32, 2 * _size(phases) * slab))
    try:
        laid = replace(
            plan,
            out_slot=key,
            out_dtype=dtypes.half,
            out_ranges=tuple(order),
            out_shape=tuple(U.extent(r) for r in order),
            out_strides=tuple(strides),
        )
        tb = {**bufs, key: t}
        runs: list = []
        for at in itertools.product(*(range(U.extent(r)) for r in phases)):
            box = {r: (v, v + 1) for r, v in zip(phases, at)}
            sub, sub_bufs, _ = _box(laid, box, tb, {})
            # The slab's padding is the box's own: its view spans it.
            v = sub_bufs[key]
            sub_bufs[key] = StorageView(t, v.offset, 2 * slab)
            runs += _vector(dev, sub, sub_bufs)
        read = Access(key, dtypes.half, tuple(zip(order, strides)), 0)
        moved = replace(
            plan, expr=X.Load(0), accesses=[read], reduce_ranges=(), reductions={}
        )
        return runs + _vector(dev, moved, tb)
    finally:
        t.free()


def _flat(dev, plan: Plan, bufs, cores):
    """Every read at the output's own index, or one value (every element's):
    N elements as 128-value rows."""
    oc = dict(zip(plan.out_ranges, _mult(plan.out_ranges)))
    oc = {r: c for r, c in oc.items() if U.extent(r) > 1}
    one = set()
    for i, a in enumerate(plan.accesses):
        coef = {r: c for r, c in a.coef if U.extent(r) > 1}
        if not coef and oc:
            one.add(i)
        elif coef != oc or a.off % 16:
            return None
    n = plan.out_n
    # The kernel stores whole 128-value rows at word-aligned addresses: a
    # part of a buffer takes it only when those rows are its own.
    ost, ooff = FM.base_of(bufs[plan.out_slot])
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    part = ooff or n * item != ost.nbytes
    if part and (n % 128 or (ooff // item) % 16):
        return None
    binds, dts, lanes = {}, [], []
    for i, a in enumerate(plan.accesses):
        if a.slot is None:
            at = FM.table(dev, a.table)
        else:
            s = bufs[a.slot]
            dt = F.np_dtype(a.dtype)
            st, _ = FM.base_of(s)
            at = FM.f16(dev, s, dt, _n(st, dt)) + a.off * 2
        if i in one:
            # The value's word, and its lane there.
            lanes.append((i, at % 32 // 2))
            at -= at % 32
        binds[f"in{i}"] = at
        dts.append("f16")
    out_at, key = _out(dev, plan, bufs)
    binds["out"] = out_at
    scope = _active_scope(dev)
    if scope is not None:
        scope.stages.append(
            (
                "flat",
                plan.expr,
                tuple(dts),
                n,
                "f16",
                tuple(lanes),
                dict(binds),
                {"plan": plan.name},
            )
        )
        _wrote(dev, plan, bufs, key)
        return []
    run = FM.generated.flat(
        dev,
        plan.expr,
        tuple(dts),
        n,
        binds,
        info={"plan": plan.name},
        scalars=tuple(lanes),
    )
    _wrote(dev, plan, bufs, key)
    return [run]


def _matmul(dev, plan: Plan, bufs) -> list:
    m, n, k = plan.mnk
    a, b = plan.a, plan.b
    if a is None or b is None:
        raise Refused("a matmul plan without its A and B reads")
    batch = [r for r in plan.out_ranges if r in plan.batch]
    order = [*batch, m, n]
    if {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1} != {
        r: c for r, c in _dense(order).items() if U.extent(r) > 1
    }:
        return _reordered(dev, plan, bufs, order)
    # An operand not [.., rows, K] row-major laid out as [batch.., rows, K]:
    # a stack over every batch range, but a B no batch reads is one shared.
    runs, tmps, bufs = [], [], dict(bufs)
    try:
        for side, acc, row in (("a", a, m), ("b", b, n)):
            whole = side == "a" or any(acc.c(r) for r in batch)
            order = [*(batch if whole else []), row, *_ks(k)]
            if {r: c for r, c in acc.coef if U.extent(r) > 1} == {
                r: c for r, c in _dense(order).items() if U.extent(r) > 1
            }:
                continue
            n_el = int(np.prod([U.extent(r) for r in order]))
            key = ("mm", side, id(plan))
            pend = _pending(dev, plan, acc, order, bufs, side)
            if pend is not None:
                # Made inside the packing's kernel (`forms.Pending`).
                tmps.append(pend)
                bufs[key] = pend
            else:
                s = None if acc.gate else bufs.get(acc.slot)
                read = ("product", side, *_read_id(acc, order))
                held = _held(dev, s, read)
                t = held or Storage(dev, max(32, 2 * n_el))
                tmps.append(t)
                bufs[key] = t
                if held is None:
                    # The product quantises f16: the layout is f16 whatever acc's type.
                    sub = _laid_plan(plan, key, acc, order)
                    runs += _vector(dev, replace(sub, name=f"{plan.name}_{side}"), bufs)
                    _hold(dev, s, read, t)
            new = Access(key, dtypes.half, tuple(_dense(order).items()), 0)
            if side == "a":
                a = new
            else:
                b = new
        return runs + _product(dev, replace(plan, a=a, b=b), bufs)
    finally:
        for t in tmps:
            t.free()


def _reordered(dev, plan: Plan, bufs, order: list) -> list:
    """A product whose output is not dense as ``[batch.., m, n]`` (a batch
    range between M and N, N not innermost, a window): into an f16
    temporary so, then moved into the output (`_moved`)."""
    key = ("mmout", id(plan))
    t = Storage(dev, max(32, 2 * _size(order)))
    t.dtype = np.float16
    sub = replace(
        plan,
        out_slot=key,
        out_dtype=dtypes.half,
        out_ranges=tuple(order),
        out_shape=tuple(U.extent(r) for r in order),
        out_strides=None,
    )
    try:
        runs = _matmul(dev, sub, {**bufs, key: t})
        tplan, tbufs, finish = _half_target(dev, plan, bufs)
        runs += _moved(dev, tplan, tbufs, t, _dense(order))
        finish()
    finally:
        t.free()
    return runs


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
    # B's padding rows quantise to zero; the output keeps its logical N.
    rows = list(plan.out_ranges[:-1])
    try:
        mm = _packing_plan(dev, plan)
        Np = mm.n
    except ValueError as e:
        raise Refused(str(e)) from None
    da, db = F.np_dtype(a.dtype), F.np_dtype(b.dtype)
    src_a = Operand("full", pitch=pa, offset=a.off)
    takes = MM.takes_f16(mm.k, mm.nk, MM.kernel_knobs(), dev.machine.machine_json)
    fa = FM.f16_rows(dev, bufs[a.slot], da, src_a, K) if takes else None
    qa = (
        fa
        if fa is not None
        else FM.mx7(dev, bufs[a.slot], da, src_a, M, K, mm, "a", bt)
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
    # Each batch's rows padded to whole quanta: the epilogue's operands and
    # output are [bt, mp] rows (none read or written past them).
    rpad = mm.mp != M
    extras, binds, runs = [], {"a": qa, "b": qb}, []
    lays: list = []  # epilogue operands laid out for the kernel, freed after the run
    try:
        for i, acc in enumerate(plan.accesses, start=1):
            o = classify(acc, rows, [n])
            if o is None and acc.slot is None:
                # A table broadcast over some rows only (a mask over a batch):
                # laid out over the output's ranges on the host.
                t = Storage(dev, max(32, 2 * plan.out_n))
                lays.append(t)
                key = ("epi", i, id(plan))
                dev.write(t.addr, _table_over(acc, plan.out_ranges).tobytes())
                t.wrote(FLAT)
                bufs = {**bufs, key: t}
                acc = Access(
                    key, dtypes.half, tuple(_dense(list(plan.out_ranges)).items()), 0
                )
                o = classify(acc, rows, [n])
            elif o is None:
                # Broadcast over some rows only (a position table over a batch):
                # laid out over the output's ranges first (a move, stride 0).
                t = Storage(dev, max(32, 2 * plan.out_n))
                lays.append(t)
                key = ("epi", i, id(plan))
                bufs = {**bufs, key: t}
                runs += _vector(dev, _laid_plan(plan, key, acc, plan.out_ranges), bufs)
                acc = Access(
                    key, dtypes.half, tuple(_dense(list(plan.out_ranges)).items()), 0
                )
                o = classify(acc, rows, [n])
            if o is None:
                raise Refused(
                    f"an epilogue operand read as no row form: strides {dict(acc.coef)}"
                )
            start = 0
            if acc.slot is not None:
                item = np.dtype(F.np_dtype(acc.dtype)).itemsize
                start = FM.base_of(bufs[acc.slot])[1] // item
            off_word = o.kind == "row" and not _takes(o, Np, start)
            if (rpad or off_word) and o.kind in ("full", "row"):
                cols = [n] if o.kind == "full" else []
                width = Np if cols else 1
                if (
                    elide_rule("padded_epilogue")
                    and bt == 1
                    and not off_word
                    and acc.slot is not None
                    and o.kind == "full"
                    and o.pitch == Np
                    and o.offset == 0
                    and start == 0
                    and RW.MUTANT is None
                ):
                    at_cand, o_cand = _addr(dev, acc, o, bufs)
                    entry = (
                        dev.mmu.entries.get(at_cand) if hasattr(dev, "mmu") else None
                    )
                    if entry is not None and entry.extent >= 2 * mm.mp * Np:
                        binds[f"in{i}"] = at_cand
                        extras.append(replace(o_cand, rows=mm.mp))
                        continue
                t, got = _pad_rows(dev, plan, bufs, acc, batch, m, cols, mm.mp, width)
                lays.append(t)
                runs += got
                nrows = bt * mm.mp
                if cols:
                    at, o = t.addr, Operand("full", pitch=Np, rows=nrows)
                else:
                    at, o = t.addr, Operand("row", rows=nrows)
                binds[f"in{i}"] = at
                extras.append(o)
                continue
            if o.kind == "col" and (o.offset % 16 or Np != N):
                if FM.PREPARED and acc.slot is not None:
                    st, off = FM.base_of(bufs[acc.slot])
                    dt = F.np_dtype(acc.dtype)
                    count = _n(st, dt)
                    at = FM.f16(dev, st, dt, count)
                    step = FM.Step(
                        at,
                        count,
                        ((N, 1),),
                        o.offset + off // np.dtype(dt).itemsize,
                        ((N, 1),),
                        0,
                        Np,
                    )
                    pending = FM.Pending(dev, 2 * Np, (step,))
                    lays.append(pending)
                    binds[f"in{i}"] = pending
                    extras.append(Operand("col"))
                    continue
                # A row not on a word: laid out Np wide, the rest zero.
                t = Storage(dev, 2 * Np)
                lays.append(t)
                if acc.slot is None:
                    laid = np.zeros(Np, np.float16)
                    laid[:N] = fp16.write(acc.table).reshape(-1)[
                        o.offset : o.offset + N
                    ]
                    dev.write(t.addr, laid.tobytes())
                elif not _gather_to(
                    dev, acc, bufs, ((N, 1),), o.offset, ((N, 1),), 0, Np, t.addr
                ):
                    raise Refused(f"an epilogue row of {N} with no f16 form")
                binds[f"in{i}"] = t.addr
                extras.append(Operand("col"))
                continue
            laid = _rows_form(bufs, acc, o, N, Np)
            if laid is not None:
                # The producer's padded rows are the rows the product reads
                # (their padding lanes go to the output's discarded columns).
                binds[f"in{i}"], o = laid
                extras.append(o)
                continue
            at, o = _addr(dev, acc, o, bufs)
            if o.kind == "full" and (o.pitch < Np or o.offset % 16 or o.pitch % 16):
                # Rows read Np wide: laid out Np apart first, the extra columns
                # zero (the mover, else `forms.relayout`; a table on the host).
                nrows = bt * M
                t = Storage(dev, 2 * nrows * Np)
                lays.append(t)
                if not ((2 * N) % 32 or o.offset % 16 or o.pitch % 16):
                    FM.repitch(dev, at + 2 * o.offset, o.pitch, t.addr, Np, nrows, N)
                elif acc.slot is None:
                    flat = fp16.write(acc.table).reshape(-1)
                    laid = np.zeros((nrows, Np), np.float16)
                    for r in range(nrows):
                        laid[r, :N] = flat[
                            o.offset + r * o.pitch : o.offset + r * o.pitch + N
                        ]
                    dev.write(t.addr, laid.tobytes())
                else:
                    walk, dwalk = ((nrows, o.pitch), (N, 1)), ((nrows, Np), (N, 1))
                    if not _gather_to(
                        dev, acc, bufs, walk, o.offset, dwalk, 0, nrows * Np, t.addr
                    ):
                        raise Refused(
                            f"an epilogue operand of rows of {N} with no f16 form"
                        )
                at, o = t.addr, Operand("full", pitch=Np, rows=nrows)
            binds[f"in{i}"] = at
            extras.append(o)
        return runs + _product_run(
            dev,
            plan,
            bufs,
            mm,
            binds,
            extras,
            bt,
            M,
            N,
            Np,
            batch,
            m,
            n,
            fa is not None,
        )
    finally:
        for t in lays:
            t.free()


def _pad_rows(dev, plan: Plan, bufs, acc, batch, m, cols, mp: int, width: int):
    """``(storage, runs)``: `acc` over ``[batch.., m, cols..]`` as f16 with
    each batch's rows padded to `mp`, rows `width` elements apart."""
    order = [*batch, m, *cols]
    strides = [width, *([1] if cols else [])]
    step = mp * width
    for r in reversed(batch):
        strides.insert(0, step)
        step *= U.extent(r)
    s = None if acc.gate else bufs.get(acc.slot)
    read = ("pad rows", *_read_id(acc, order), tuple(strides), mp)
    held = _held(dev, s, read)
    if held is not None:
        return held, []
    t = Storage(dev, max(32, 2 * step))
    t.wrote(FLAT)
    t.dtype = np.float16
    if acc.slot is None:
        laid = np.zeros(step, np.float16)
        idx = np.zeros([U.extent(r) for r in order], np.int64)
        for i, (r, s) in enumerate(zip(order, strides)):
            shape = [1] * len(order)
            shape[i] = U.extent(r)
            idx = idx + s * np.arange(U.extent(r)).reshape(shape)
        laid[idx] = _table_over(acc, order)
        dev.write(t.addr, laid.tobytes())
        return t, []
    key = ("pad_rows", id(acc), id(plan))
    try:
        runs = _vector(
            dev, _laid_plan(plan, key, acc, order, strides), {**bufs, key: t}
        )
        return _hold(dev, s, read, t), runs
    except BaseException:
        t.free()
        raise
