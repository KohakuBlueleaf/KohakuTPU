"""Running a plan on the device: each read classified as a `vector.Operand`
against the kernel's rows and columns, its form made (`runtime.forms`), the
generated DSL kernel compiled (cached) and run, the output's form recorded.

Where the forms are is known only when the kernel runs (a statistic a
reduction left is not row-major bytes), so the last choices are made here:
a kernel whose operands are statistics reads compact [n] values, 16 a
word; a row-major one reads the bytes.
"""

import itertools
import os
from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import transpose as TP
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import launch_schedule
from kohakutpu.application.ktpugrad import lower as L
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad import window as W
from kohakutpu.application.ktpugrad.launch_config import (
    _active_scope,
    elide_contiguous_enabled,
    elide_rule,
    horizontal_fusion_enabled,
    knobs,
    multi_stage_scope,
    single_package_enabled,
    view_forms_enabled,
)
from kohakutpu.application.ktpugrad.launch_emit import flush_multi_stage
from kohakutpu.application.ktpugrad.launch_views import _remap_view_access
from kohakutpu.application.ktpugrad.lower import Access, Plan, Refused
from kohakutpu.application.ktpugrad.memo import memo
from kohakutpu.application.ktpugrad.window import tabled
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import tree as TR
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView
from kohakutpu.language import machine as MF
from kohakutpu.language.numerics import fp16

#: Launch-reduction knobs (each configurable via `knobs(...)` or environment).
VIEW_FORMS = False
ELIDE_CONTIGUOUS = True
HORIZONTAL_FUSION = True


def _reform_one(dev, plan: Plan, a: Access | None, bufs: dict) -> Access | None:
    if a is None or a.slot is None:
        return a
    s = bufs.get(a.slot)
    st, off = FM.base_of(s) if s is not None else (None, 0)
    if st is None or off or not dtypes.is_float(a.dtype):
        return a
    if view_forms_enabled():
        vkey = FM.viewed(st)
        if (
            vkey is not None
            and a.slot != plan.out_slot
            and not isinstance(plan.expr, X.Load)
        ):
            remapped = _remap_view_access(a, vkey)
            if remapped is not None:
                coef, new_off = remapped
                if RW.MUTANT == "wrong_order" and len(coef) >= 2:
                    coef = tuple(reversed(coef))
                alias = ("view", a.slot)
                if alias not in bufs:
                    vf = st.forms[vkey]
                    bufs[alias] = Storage(dev, vf.nbytes, addr=vf.addr, owned=False)
                return replace(a, slot=alias, coef=coef, off=new_off, dtype=dtypes.half)
    key = FM.padded(st)
    allow_self = view_forms_enabled() and plan.kind == "vector"
    if key is None or (a.slot == plan.out_slot and not allow_self):
        return a
    _, n, np_ = key
    inner = [(r, c) for r, c in a.coef if c % n and U.extent(r) > 1]
    span = a.off % n + sum(c * (U.extent(r) - 1) for r, c in inner)
    if any(c < 0 for _, c in inner) or span >= n:
        return a
    coef = tuple((r, c if c % n else c // n * np_) for r, c in a.coef)
    off_el = a.off // n * np_ + a.off % n
    alias = ("rows", a.slot)
    if alias not in bufs:
        f = st.forms[key]
        bufs[alias] = Storage(dev, f.nbytes, addr=f.addr, owned=False)
    # The form holds f16 values whatever the buffer's element type.
    return replace(a, slot=alias, coef=coef, off=off_el, dtype=dtypes.half)


def _reform(dev, plan: Plan, bufs: dict) -> tuple:
    """Reads of a buffer held as padded rows (rows of n, np apart) or a view
    descriptor: each read through an alias of that form with its strides
    remapped; a read that crosses a row stays as it is."""
    bufs = dict(bufs)
    accesses = [_reform_one(dev, plan, a, bufs) for a in plan.accesses]
    return replace(plan, accesses=accesses), bufs


def execute(dev, plan: Plan, bufs: dict):
    """Run `plan` with `bufs` (param slot -> storage); returns the runs."""
    if (
        view_forms_enabled()
        and plan.kind == "vector"
        and not plan.reduce_ranges
        and isinstance(plan.expr, X.Load)
        and plan.out_strides is None
        and len(plan.accesses) == 1
    ):
        acc = plan.accesses[0]
        if (
            acc.slot is not None
            and acc.slot in bufs
            and dtypes.is_float(acc.dtype)
            and dtypes.is_float(plan.out_dtype)
        ):
            sst, soff = FM.base_of(bufs[acc.slot])
            dst, doff = FM.base_of(bufs[plan.out_slot])
            out_item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
            pkey = FM.padded(sst) if not soff else None
            if (
                pkey is not None
                and not doff
                and acc.off == 0
                and plan.out_n * out_item == dst.nbytes
                and {r: c for r, c in acc.coef if U.extent(r) > 1}
                == {r: c for r, c in _dense(plan.out_ranges).items() if U.extent(r) > 1}
                and RW.MUTANT != "drop_member"
            ):
                pf = sst.forms[pkey]
                target = sst if RW.MUTANT == "alias_members" else dst
                target.share_form(pkey, pf.addr, pf.nbytes, src_st=sst)
                target.wrote(pkey)
                target.dtype = F.np_dtype(plan.out_dtype)
                if pkey in sst.zeroed:
                    target.zeroed.add(pkey)
                return []
    if plan.kind not in ("vector", "copy"):
        return _execute(dev, plan, bufs)
    if plan.kind == "copy" and view_forms_enabled():
        runs = _execute(dev, plan, bufs)
        scope = _active_scope(dev)
        if scope is not None and scope.remaining is not None:
            scope.remaining -= 1
            if scope.remaining <= 0:
                runs += flush_multi_stage(scope)
        return runs
    plan, aliased = _reform(dev, plan, bufs)
    if plan.kind == "copy" and any(
        a.slot != plan.out_slot and isinstance(a.slot, tuple) for a in plan.accesses
    ):
        plan = replace(plan, kind="vector")
    try:
        runs = _execute(dev, plan, aliased)
        scope = _active_scope(dev)
        if scope is not None and scope.remaining is not None:
            scope.remaining -= 1
            if scope.remaining <= 0:
                runs += flush_multi_stage(scope)
        return runs
    finally:
        # An alias owns no bytes, only the forms a kernel made of it.
        for k, s in aliased.items():
            if k not in bufs:
                s.free()


def _execute(dev, plan: Plan, bufs: dict):
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


def _n(st, dtype) -> int:
    return st.nbytes // np.dtype(dtype).itemsize


def _addr(dev, acc, o: Operand, bufs) -> tuple:
    """``(address, operand)`` of a read in its f16 form: a value a row or one
    value from its base's (the sub-buffer's offset in the operand's, so the
    kernel reads whole words), rows of values from the view's."""
    if acc.slot is None:
        return FM.table(dev, acc.table), o
    s = bufs[acc.slot]
    dt = F.np_dtype(acc.dtype)
    st, off = FM.base_of(s)
    if o.kind in ("row", "scalar"):
        at = FM.f16(dev, st, dt, _n(st, dt))
        return at, replace(o, offset=o.offset + off // np.dtype(dt).itemsize)
    return FM.f16(dev, s, dt, _n(st, dt) if not off else s.nbytes // dt.itemsize), o


def _out(dev, plan: Plan, bufs) -> tuple:
    """``(address, key)`` the kernel writes the output to: the f16 bytes of
    an f16 buffer, else its f16 form (the rest current first)."""
    s = bufs[plan.out_slot]
    st, off = FM.base_of(s)
    n = plan.out_n
    dt = np.dtype(F.np_dtype(plan.out_dtype))
    if dt == np.float16:
        return st.addr + off, FLAT
    total = st.nbytes // dt.itemsize
    if (off or n != total) and (st.dtype is not None or st.host is not None):
        # Part of a written buffer: the rest kept, in its f16 form.
        FM.f16(dev, st, dt, total)
    return st.form(FM.F16, 2 * total).addr + off // dt.itemsize * 2, FM.F16


def _wrote(dev, plan: Plan, bufs, key) -> None:
    st, _ = FM.base_of(bufs[plan.out_slot])
    st.wrote(key)
    st.dtype = F.np_dtype(plan.out_dtype)


# ----------------------------------------------------------------- copy
def _copy(dev, plan: Plan, bufs) -> list:
    """A buffer's current form copied whole by the copy kernel: its bytes
    word to word, or its f16 / statistic / padded-rows form; any other copy
    (a sub-buffer off a word, a form of part of a buffer) is the bare read it
    is (`_vector`)."""
    acc = plan.accesses[0]
    src, dst = bufs[acc.slot], bufs[plan.out_slot]
    sst, soff = FM.base_of(src)
    dst_, doff = FM.base_of(dst)
    key = next((k for k in (FLAT, FM.F16) if k in sst.valid), None)
    key = key or FM.padded(sst)
    if key is None:
        raise Refused("a copy of a buffer with no current form")
    if key == FLAT:
        n = plan.out_n * np.dtype(F.np_dtype(plan.out_dtype)).itemsize
        a, b = sst.addr + soff, dst_.addr + doff
        whole = not (a % 32 or b % 32)
    else:
        n = sst.forms[key].nbytes
        a, b = sst.forms[key].addr, 0
        whole = not (soff or doff)
    if not whole:
        return _vector(dev, replace(plan, kind="vector"), bufs)
    if key != FLAT:
        b = dst_.form(key, n).addr
    if elide_rule("self_copy") and a == b and RW.MUTANT != "drop_member":
        dst_.wrote(key)
        dst_.dtype = F.np_dtype(plan.out_dtype)
        return []
    scope = _active_scope(dev)
    if scope is not None:
        scope.stages.append(("copy", int(n), int(a), int(b), {"plan": plan.name}))
        dst_.wrote(key)
        dst_.dtype = F.np_dtype(plan.out_dtype)
        if key == FLAT and sst.host is not None and not soff and not doff:
            dst_.host = sst.host
        return []
    run = FM.generated.copy(dev, n, {"src": a, "dst": b}, info={"plan": plan.name})
    dst_.wrote(key)
    dst_.dtype = F.np_dtype(plan.out_dtype)
    if key == FLAT and sst.host is not None and not soff and not doff:
        dst_.host = sst.host
    return [run]


# --------------------------------------------------------------- vector
def _all_reduces(e, out=None) -> list:
    """Every `Reduce` in `e` (a node the expression shares once), inner ones
    too."""
    out = [] if out is None else out
    if isinstance(e, X.Reduce):
        if any(r is e for r in out):
            return out
        out.append(e)
        _all_reduces(e.arg, out)
    elif isinstance(e, X.Op):
        for a in e.args:
            _all_reduces(a, out)
    return out


def _rebuild(e, f):
    """`e` rebuilt, `f(x, walk)` first given each node (its result used when
    not None; `walk` rebuilds a child); a node the expression shares stays
    one node."""
    memo: dict = {}

    def walk(x):
        if id(x) in memo:
            return memo[id(x)]
        got = f(x, walk)
        if got is None:
            if isinstance(x, X.Op):
                got = X.Op(x.name, tuple(walk(a) for a in x.args))
            elif isinstance(x, X.Reduce):
                got = X.Reduce(x.op, walk(x.arg))
            else:
                got = x
        memo[id(x)] = got
        return got

    return walk(e)


def _prune(e, accesses: list) -> tuple:
    """`e` reading only the accesses it uses, renumbered in order of use."""
    used: dict = {}
    remap: dict = {}

    def f(x, walk):
        if isinstance(x, X.Load):
            if x.i not in used:
                used[x.i] = len(used)
            return X.Load(used[x.i])
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, walk(x.arg))
            remap[id(new)] = id(x)
            return new
        return None

    e2 = _rebuild(e, f)
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
    at = soff + acc.off * item
    if doff % 32 or not _words(plan, acc, item, at):
        return None
    levels = _walk(plan, dict(acc.coef), item)
    dlevels = _walk(plan, _out_coef(plan), item)
    assert levels is not None and dlevels is not None
    _window_ok(dev, dst, plan)
    out_item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    if (
        elide_rule("identity_walk")
        and base + at == dst.addr + doff
        and tuple(levels) == tuple(dlevels)
        and len(levels) == 1
        and RW.MUTANT is None
    ):
        _wrote_flat(dst, plan)
        return []
    if (
        view_forms_enabled()
        and isinstance(plan.out_slot, int)
        and isinstance(acc.slot, int)
        and not doff
        and plan.out_strides is None
        and item == 2
        and dtypes.is_float(plan.out_dtype)
        and plan.out_n * out_item == dst.nbytes
        and tuple(dlevels) == ((-(-dst.nbytes // 32), 32),)
        and base != dst.addr
        and _active_scope(dev) is None
    ):
        if RW.MUTANT == "drop_member":
            return []
        walk_lvls = (
            tuple(reversed(levels))
            if RW.MUTANT == "wrong_order" and len(levels) > 1
            else tuple(levels)
        )
        at_use = (
            at + 32
            if RW.MUTANT == "wrong_order" and len(levels) == 1 and at + 32 < nbytes
            else at
        )
        coef_map = dict(acc.coef)
        coef_ext = tuple((U.extent(r), coef_map.get(r, 0)) for r in plan.out_ranges)
        vkey = FM.view_key(
            nbytes,
            walk_lvls,
            at_use,
            plan.out_shape,
            coef_ext,
            acc.off,
        )
        sst_base = FM.base_of(bufs[acc.slot])[0]
        target = sst_base if RW.MUTANT == "alias_members" else dst
        target.share_form(vkey, base, nbytes, src_st=sst_base)
        target.wrote(vkey)
        target.dtype = F.np_dtype(plan.out_dtype)
        return []
    run = FM.generated.walk(
        dev,
        nbytes,
        levels,
        at,
        dst.nbytes,
        dlevels,
        doff,
        {"src": base, "dst": dst.addr},
        "move",
        info={"plan": plan.name},
    )
    _wrote_flat(dst, plan)
    return [run]


def _words(plan: Plan, acc: Access, item: int, at: int) -> bool:
    """Whether the mover walks read `acc` (from byte `at`, `item`-byte
    elements) into `plan`'s output in whole 32-byte words."""
    return (
        not at % 32
        and at >= 0
        and _walk(plan, dict(acc.coef), item) is not None
        and _walk(plan, _out_coef(plan), item) is not None
    )


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
    if A % 16:
        return None  # rows not of whole words: `_gather`
    # A side of B not whole words is read 16-padded (the extra lanes, inside
    # the buffer, dropped after): a narrow channel count's NHWC -> NCHW.
    Bp = -(-B // 16) * 16
    rsp = (
        r_s if Bp == B else r_s.replace(src=(r_s.src[0].replace(arg=Bp), *r_s.src[1:]))
    )
    others = [r for r in live if r not in (r_in, r_s)]
    nb = int(np.prod([U.extent(r) for r in others])) if others else 1
    n = nb * A * Bp
    runs: list = []
    found = _source(dev, plan, acc, bufs)
    if found is None:
        return None
    base, nbytes, item, off = found
    if Bp != B:
        last = off // item + acc.off + Bp - 1
        last += sum(
            c * (U.extent(r) - 1) for r, c in acc.coef if r is not r_s and c > 0
        )
        if last >= nbytes // item:
            return None
    read1 = replace(acc, coef=tuple((rsp if r is r_s else r, c) for r, c in acc.coef))
    order1 = [*others, r_in, rsp]
    sub = replace(
        plan,
        name=f"{plan.name}_p",
        out_slot=("tr1", id(plan)),
        out_dtype=dtypes.half,
        out_ranges=tuple(order1),
        out_shape=tuple(U.extent(r) for r in order1),
        out_strides=None,
        accesses=[read1],
    )
    staged = not (
        Bp == B
        and {r: c for r, c in acc.coef if U.extent(r) > 1} == _dense(order1)
        and item == 2
        and not (off + 2 * acc.off) % 32
    )
    order2 = [*others, r_s, r_in]
    dst, doff = FM.base_of(bufs[plan.out_slot])
    oc = {r: c for r, c in _out_coef(plan).items() if U.extent(r) > 1}
    direct = Bp == B and oc == _dense(order2) and not doff % 32
    # The first B of each Bp rows, read back into the output.
    coef2 = _dense([*others, rsp, r_in])
    key2 = ("tr2", id(plan))
    read = Access(
        key2,
        dtypes.half,
        tuple((r_s if r is rsp else r, c) for r, c in coef2.items()),
        0,
    )
    post = replace(plan, expr=X.Load(0), accesses=[read])
    # Every move in whole words before anything runs; else not one.
    if staged and not _words(sub, read1, item, off + item * acc.off):
        return None
    if not direct and (doff % 32 or not _words(post, read, 2, 0)):
        return None
    tmps = []
    try:
        if not staged:
            src = base + off + 2 * acc.off
        else:
            t1 = Storage(dev, 2 * n)
            tmps.append(t1)
            got = _move(dev, sub, {**bufs, sub.out_slot: t1})
            assert got is not None
            runs += got
            src = t1.addr
        if direct:
            _window_ok(dev, dst, plan)
            out = dst.addr + doff
        else:
            t2 = Storage(dev, 2 * n)
            tmps.append(t2)
            out = t2.addr
        runs.append(
            FM.generated.launch(
                dev,
                "transpose.transpose_kernel",
                {"nb": int(nb), "A": int(A), "B": int(Bp)},
                {"x": src, "mask": FM.table(dev, TP.masks()), "y": out},
                kind="transpose",
                info={"plan": plan.name},
            )
        )
        if direct:
            _wrote_flat(dst, plan)
            return runs
        got = _move(dev, post, {**bufs, key2: tmps[-1]})
        assert got is not None
        return runs + got
    finally:
        for t in tmps:
            t.free()


def _gather(dev, plan: Plan, bufs) -> list | None:
    """Any read into an f16 output, element by element: the source's f16
    elements walked into the output's (`forms.relayout`, exact). A dense output from
    a word is written whole; any other (a window, an offset) keeps what its
    words held between its elements. None for a table, a non-f16 output or
    a buffer with no f16 form."""
    acc = plan.accesses[0]
    if acc.slot is None or plan.out_dtype != dtypes.half:
        return None
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    n = _size(live)
    item = np.dtype(F.np_dtype(acc.dtype)).itemsize
    if FM.base_of(bufs[acc.slot])[1] // item + acc.off < 0:
        return None
    coef, oc = dict(acc.coef), _out_coef(plan)
    walk = tuple((U.extent(r), coef.get(r, 0)) for r in live)
    dwalk = tuple((U.extent(r), oc[r]) for r in live)
    view = bufs[plan.out_slot]
    dst, doff = FM.base_of(view)
    # The gather writes whole words: a dense output owns the last one's rest
    # (a whole buffer ends in its allocation's tail).
    whole = (
        {r: oc[r] for r in live} == _dense(live)
        and doff % 32 == 0
        and (
            n % 16 == 0
            or view.nbytes >= 2 * -(-n // 16) * 16
            or (not doff and dst.nbytes == 2 * n)
        )
    )
    site = ("gather", acc.dtype, walk, acc.off, dwalk)
    held = None
    if whole and FM.hints(dev).get(site) == "rows":
        held = _rows_out(dst, doff, dwalk, n)
    if held is not None:
        # Its consumers read its rows padded to whole words (`forms.hints`):
        # written so, zero-padded, whole lines.
        key, at, dwalk, m = held
        if not _gather_to(dev, acc, bufs, walk, acc.off, dwalk, 0, m, at):
            return None
        dst.wrote(key)
        dst.zeroed.add(key)
        dst.dtype = F.np_dtype(plan.out_dtype)
        dst.site = (site, dwalk[-1][0])
        return []
    at = dst.addr + doff // 32 * 32
    dat = doff % 32 // 2
    m = n if whole else -(-(dat + 1 + sum((e - 1) * s for e, s in dwalk)) // 16) * 16
    if not whole:
        _window_ok(dev, dst, plan)
    if not _gather_to(dev, acc, bufs, walk, acc.off, dwalk, dat, m, at, keep=not whole):
        return None
    _wrote_flat(dst, plan)
    if whole and dwalk and dwalk[-1][1] == 1:
        dst.site = (site, dwalk[-1][0])
    return []


#: Values a held row is padded to a multiple of: whole memory lines.
ROW_ALIGN = 32


def _rows_out(dst: Storage, doff: int, dwalk, n: int) -> tuple | None:
    """``(form key, address, walk, values)`` of a dense output of a whole
    buffer held as padded rows: its innermost axis a row of w values (not
    whole words) padded to whole lines, each outer step of w one of the
    pitch; None when its rows are whole words or it is part of a buffer."""
    if doff or dst.nbytes != 2 * n or not dwalk or dwalk[-1][1] != 1:
        return None
    w = dwalk[-1][0]
    if w % 16 == 0 or n % w:
        return None
    wp = -(-w // ROW_ALIGN) * ROW_ALIGN
    walk = tuple((e, s // w * wp) for e, s in dwalk[:-1]) + (dwalk[-1],)
    if any(s % w for _, s in dwalk[:-1]):
        return None
    key = FM.rows_key(w, wp)
    m = n // w * wp
    return key, dst.form(key, 2 * m).addr, walk, m


def _gather_to(dev, acc, bufs, walk, first, dwalk, dat, n, dst, keep=False) -> bool:
    """`n` f16 values at `dst` from `acc`'s buffer as f16 (`forms.relayout`):
    element `first` (from `acc`'s view) walked by `walk` into elements
    walked by `dwalk` from `dat`, every other one zero (with `keep`, as `dst`
    held it). False when the buffer has no f16 form."""
    st, off = FM.base_of(bufs[acc.slot])
    dt = F.np_dtype(acc.dtype)
    N = _n(st, dt)
    first += off // np.dtype(dt).itemsize
    try:
        src = FM.f16(dev, st, dt, N)
    except FM.FormError:
        return False
    FM.relayout(dev, src, N, walk, first, dwalk, n, dst, dat, keep)
    return True


@memo
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
    key = ("flip", id(plan))
    post = replace(
        plan, accesses=[Access(key, dtypes.half, tuple(_dense(live).items()), 0)]
    )
    if not whole and (doff % 32 or not _words(post, post.accesses[0], 2, 0)):
        return None  # its output not whole words: `_gather`
    t = None if whole else Storage(dev, 2 * n)
    try:
        arguments = {"src_bytes": st.nbytes, "levels": levels, "at": start, "n": n}
        out = dst.addr if t is None else t.addr
        runs = [
            FM.generated.launch(
                dev,
                "transpose.reverse_kernel",
                arguments,
                {"x": st.addr, "y": out},
                kind="flip",
                info={"plan": plan.name},
            )
        ]
        if t is None:
            _wrote_flat(dst, plan)
            return runs
        moved = _move(dev, post, {**bufs, key: t})
        assert moved is not None
        return runs + moved
    finally:
        if t is not None:
            t.free()


def _window_ok(dev, dst: Storage, plan: Plan) -> None:
    """A plan writing part of `dst` keeps the rest: its flat bytes made
    current first (an f16 buffer's from its current form, `forms.f16`)."""
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    if FLAT in dst.valid or (
        plan.out_strides is None and plan.out_n * item == dst.nbytes
    ):
        return
    if dst.dtype is None or np.dtype(dst.dtype) == np.float16:
        FM.f16(dev, dst, np.float16, dst.nbytes // 2)
        return
    raise Refused(
        f"part of a {np.dtype(dst.dtype)} buffer written while its bytes are not "
        "current (its f16 form is)"
    )


def _wrote_flat(dst: Storage, plan: Plan) -> None:
    dst.wrote(FLAT)
    dst.dtype = F.np_dtype(plan.out_dtype)


def _out_coef(plan: Plan) -> dict:
    strides = plan.out_strides or tuple(_mult(plan.out_ranges))
    return dict(zip(plan.out_ranges, strides))


def _fill(dev, plan: Plan, bufs, value: float) -> list:
    """A box of the loop nest that is one value (a padding): the mover's
    fill over the output window where it is whole words, else the value
    moved in from a temporary (`_moved`: any window)."""
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    dst, doff = FM.base_of(bufs[plan.out_slot])
    dlevels = _walk(plan, _out_coef(plan), item)
    if dlevels is None or doff % 32 or item == 8:
        live = _live(plan.out_ranges)
        t = Storage(dev, max(32, 2 * _size(live)))
        t.dtype = np.float16
        try:
            dev.write(
                t.addr, fp16.write(np.full(_size(live), value, np.float64)).tobytes()
            )
            tplan, tbufs, finish = _half_target(dev, plan, bufs)
            runs = _moved(dev, tplan, tbufs, t, _dense(live))
            finish()
        finally:
            t.free()
        return runs
    dtype = F.np_dtype(plan.out_dtype)
    encoded = fp16.write(value) if dtype == np.float16 else np.asarray(value, dtype)
    bits = encoded.reshape(1).view({1: np.uint8, 2: np.uint16, 4: np.uint32}[item])
    _window_ok(dev, dst, plan)
    run = FM.generated.fill(
        dev,
        dst.nbytes,
        dlevels,
        doff,
        bits[0],
        8 * item,
        {"dst": dst.addr},
        info={"plan": plan.name},
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

    def fold(x, walk):
        if isinstance(x, X.Load):
            return X.Const(consts[x.i]) if x.i in consts else x
        if isinstance(x, X.Op):
            args = tuple(walk(a) for a in x.args)
            if x.name == "where" and isinstance(args[0], X.Const):
                return args[1] if args[0].value else args[2]
            return X.Op(x.name, args)
        if isinstance(x, X.Reduce):
            out = X.Reduce(x.op, walk(x.arg))
            reductions[id(out)] = tuple(new.get(r, r) for r in plan.reductions[id(x)])
            return out
        return None

    e = _rebuild(plan.expr, fold)
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
    # Dense when the ranges left (a box of one value has no stride) are.
    live = [(r, new.get(r, r)) for r in plan.out_ranges if U.extent(new.get(r, r)) > 1]
    dense = _dense([n for _, n in live])
    if all(mult[r] == dense[n] for r, n in live):
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


def _size(rs) -> int:
    return int(np.prod([U.extent(r) for r in rs])) if rs else 1


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


#: Whether a window product may run with its sides swapped (B's outer ranges
#: its rows) when that is the output's order.
WINDOW_SWAP = os.environ.get("KTPU_WINDOW_SWAP", "0") == "1"


def _window_swapped(plan: Plan, c) -> bool:
    """Whether window contraction `c` runs as ``[batch.., B outer.., A
    outer..]``: the output's innermost range is one of A's outer ranges
    (normally the product's rows), so the product in its own order would
    reach the output only transposed."""
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    ma = {r for r in c.ma if U.extent(r) > 1}
    return WINDOW_SWAP and bool(live) and live[-1] in ma


def _window_lead(plan: Plan, c) -> list:
    """A's outer ranges before every B outer range in the output's order:
    a swapped product's batch ranges (B, not reading them, read the same in
    each batch)."""
    live = [r for r in plan.out_ranges if U.extent(r) > 1]
    nb = [live.index(r) for r in c.nb if r in live]
    if not nb:
        return []
    first = min(nb)
    return [r for r in c.ma if r in live and live.index(r) < first]


def _lay(dev, plan: Plan, bufs, tmps, c, side, outer, lane, key, defer=None) -> list:
    """`side`'s read dense at ``bufs[key]`` as f16 ``[outer.., c.ks..,
    lane]``: its padded source (`_padded`) walked by the mover, every step
    whole words (the lane innermost, whole words). Both steps a
    `forms.Pending` for the packing's kernel when `forms.PREPARED`."""
    if defer is None:
        defer = FM.PREPARED and FM.PREPARED_QUANT
    src, strides, off, step = _padded(
        dev, plan, bufs, tmps, c, side, side is c.a, defer
    )
    order = [*outer, *c.ks, lane]
    held = {r for d in side.dims for r in d.ranges()}
    # A range the side does not read (a swapped product's batch): stride 0.
    coef = {
        r: side.mult(r) * strides[side.dim(r)] if r in held else 0
        for r in [*outer, *c.ks]
    }
    coef[lane] = 1
    if step is None and coef == {r: s for r, s in _dense(order).items()} and off == 0:
        bufs[key] = bufs[src]
        return []
    if defer:
        live = [r for r in order if U.extent(r) > 1]
        dense = _dense(live)
        walk = tuple((U.extent(r), coef[r]) for r in live)
        dwalk = tuple((U.extent(r), dense[r]) for r in live)
        n = _size(live)
        if step is not None:
            steps = (
                (step,)
                if walk == dwalk and off == 0 and step.m == n
                else (step, FM.Step(None, step.m, walk, off, dwalk, 0, n))
            )
        else:
            st, voff = FM.base_of(bufs[src])
            N = st.nbytes // 2
            at = FM.f16(dev, st, np.float16, N)
            steps = (FM.Step(at, N, walk, off + voff // 2, dwalk, 0, n),)
        if not FM.costs.has_steps(steps, dev.machine.board):
            return _lay(dev, plan, bufs, tmps, c, side, outer, lane, key, defer=False)
        pend = FM.Pending(dev, 2 * max(n, 16), steps)
        tmps.append(pend)
        bufs[key] = pend
        return []
    t = Storage(dev, max(32, 2 * _size(order)))
    tmps.append(t)
    t.wrote(FLAT)
    bufs[key] = t
    sub = Plan(
        "vector",
        plan.ast,
        f"{plan.name}_w",
        key,
        dtypes.half,
        tuple(U.extent(r) for r in order),
        tuple(order),
        expr=X.Load(0),
        accesses=[Access(src, dtypes.half, tuple(coef.items()), off)],
    )
    runs = _move(dev, sub, bufs)
    if runs is None:
        raise Refused("a window operand not walked in whole words")
    return runs


def _padded(dev, plan: Plan, bufs, tmps, c, side, copies=False, defer=False) -> tuple:
    """``(slot, {coordinate: element stride}, element of coordinate 0,
    step)`` of `side`'s source with its lane innermost in whole words and
    zero outside its gate's box: the buffer itself when it is so already
    (step None), else gathered (`_gather_to`) into ``[coordinates.., lane]``
    -- with `defer`, that gather's `forms.Step` (slot None), not run. A lane
    of one with `copies` (the other side's lanes zero) holds 16 copies of
    each value (`_lane_copies`): no transpose."""
    acc = side.acc
    axes = [d for d in side.dims if d is not side.lane]
    full = all(d.box == (0, d.span) for d in side.dims)
    st, voff = FM.base_of(bufs[acc.slot])
    lane = side.lane
    if (
        full
        and lane is not None
        and lane.stride == 1
        and U.extent(lane.ranges()[0]) == c.pad
        and all(d.stride % W.WORD == 0 for d in axes)
        and (voff // 2 + acc.off) % W.WORD == 0
        and acc.dtype == dtypes.half
        and FLAT in st.valid
    ):
        return acc.slot, {d: d.stride for d in side.dims}, acc.off, None
    key = ("pad", id(side), id(plan))
    return _pad_copy(dev, bufs, tmps, acc, axes, key, lane, c.pad, copies, defer)


def _pad_copy(
    dev, bufs, tmps, acc, axes, key, lane=None, pad=1, copies=False, defer=False
):
    """``(key, {coordinate: element stride}, element of coordinate 0, step)``:
    `acc`'s source over its coordinates (`axes` outermost first, then the
    `lane` innermost, `pad` elements wide) gathered dense as f16 into
    ``bufs[key]``, every element outside the coordinates' boxes zero (with
    `defer`, that gather as a `forms.Step`, key None, nothing run). A lane
    of one with `copies` holds 16 copies of each value (`_lane_copies`)."""
    ext = [d.span for d in axes] + [pad]
    dense = [int(np.prod(ext[i + 1 :])) for i in range(len(ext))]
    strides = dict(zip(axes, dense))
    if lane is not None:
        strides[lane] = 1
    walk, dwalk = [], []
    first, dat = acc.off, 0
    for d in axes + ([lane] if lane is not None else []):
        a, b = d.box
        walk.append((b - a, d.stride))
        dwalk.append((b - a, strides[d]))
        first += d.stride * (d.lo + a)
        dat += strides[d] * a
    n = int(np.prod(ext))
    origin = -sum(strides[d] * d.lo for d in axes)
    if defer:
        st, off = FM.base_of(bufs[acc.slot])
        dt = F.np_dtype(acc.dtype)
        if lane is None and copies:
            walk, dwalk = [*walk, (16, 0)], [*dwalk, (16, 1)]
        first += off // np.dtype(dt).itemsize
        try:
            src, N, w, first, dw = _f16_walk(
                dev, st, dt, tuple(walk), first, tuple(dwalk)
            )
        except FM.FormError:
            raise Refused("a padded operand with no f16 form") from None
        m = -(-n // 16) * 16
        step = FM.Step(src, N, w, first, dw, dat, m)
        return None, strides, origin, step
    s = Storage(dev, 2 * -(-n // 16) * 16)
    tmps.append(s)
    if lane is None and copies:
        _lane_copies(dev, acc, bufs, walk, first, dwalk, dat, s)
    elif not _gather_to(
        dev, acc, bufs, tuple(walk), first, tuple(dwalk), dat, n, s.addr
    ):
        raise Refused("a padded operand with no f16 form")
    s.wrote(FLAT)
    s.dtype = np.float16
    bufs[key] = s
    return key, strides, origin, None


def _lane_copies(dev, acc, bufs, walk, first, dwalk, dat, dst: Storage) -> None:
    """`dst` (16-lane words, positions `dwalk` elements apart from `dat`)
    := each value `walk` reaches from `first` in all 16 lanes, every other
    word zero (`forms.relayout`, each value read 16 times)."""
    st, off = FM.base_of(bufs[acc.slot])
    dt = F.np_dtype(acc.dtype)
    N = _n(st, dt)
    first += off // np.dtype(dt).itemsize
    src = FM.f16(dev, st, dt, N)
    copies = (*walk, (16, 0)), (*dwalk, (16, 1))
    FM.relayout(
        dev, src, N, copies[0], first, copies[1], dst.nbytes // 2, dst.addr, dat
    )


@memo
def _vector_choice(plan: Plan):
    """Pure topology prefix; binding and form transitions remain per call."""
    win = W.find(plan) if plan.reduce_ranges else None
    if win is not None:
        return "window", win
    tab = tabled(plan) if plan.reduce_ranges else None
    if tab is not None:
        return "tabled", tab
    phases = _table_only(plan) if plan.reduce_ranges else []
    if phases:
        return "enumerated", phases
    mm = _contraction(plan)
    if mm is not None:
        return "matmul", mm
    if _epilogue_contraction(plan):
        return "materialise", None
    return "general", None


def _vector(dev, plan: Plan, bufs) -> list:
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
    if not plan.reduce_ranges and plan.out_strides is None:
        cores = dev.machine.count("VC")
        flat = _flat(dev, plan, bufs, cores)
        if flat is not None:
            return flat
    if not plan.reduce_ranges:
        plan = _refold(plan) or plan
    return _rows(dev, plan, bufs)


def _bare(dev, plan: Plan, bufs) -> list | None:
    """A bare read into the output (`plan.expr` one `Load`): the mover's word
    walk, a transpose, a flip, else element by element (`_gather`); None
    where none is one (a table, a non-f16 output no word walk takes)."""
    one = _coalesce(plan)
    for way in (_move, _transpose, _flip, _gather):
        got = way(dev, one, bufs)
        if got is not None:
            return got
    return None


# ------------------------------------------------------------ rows path
#: What a padding lane holds so a reduction ignores it.
IDENTITY = {"sum": 0.0, "max": -65504.0, "prod": 1.0}


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
        if kind == "along" and Wp != W:
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
        for t in tmps:
            t.free()
    return runs


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


def _half_target(dev, plan: Plan, bufs) -> tuple:
    """``(plan, bufs, finish)``: `plan` writing f16 values -- as it is for an
    f16 output, else into its output's f16 form (the rest of the buffer
    current first when it writes part of a written one); `finish()` records
    the form written."""
    st, off = FM.base_of(bufs[plan.out_slot])
    dt = np.dtype(F.np_dtype(plan.out_dtype))
    if dt == np.float16:
        return plan, bufs, lambda: None
    total = st.nbytes // dt.itemsize
    part = off or plan.out_strides is not None or plan.out_n != total
    if part and (st.dtype is not None or st.host is not None):
        FM.f16(dev, st, dt, total)
    f = st.form(FM.F16, 2 * total)
    alias = Storage(dev, f.nbytes, addr=f.addr, owned=False)
    key = ("f16", plan.out_slot)
    nbytes = bufs[plan.out_slot].nbytes // dt.itemsize * 2
    view = StorageView(alias, off // dt.itemsize * 2, nbytes)

    def finish() -> None:
        st.wrote(FM.F16)
        st.dtype = dt

    out = replace(plan, out_slot=key, out_dtype=dtypes.half)
    return out, {**bufs, key: view}, finish


def _moved(dev, plan: Plan, bufs, t: Storage, coef: dict) -> list:
    """`t` (f16 values at element strides `coef` over `plan`'s output
    ranges) moved into `plan`'s f16 output by the bare-read kernels."""
    key = ("placed", id(plan))
    read = Access(key, dtypes.half, tuple(coef.items()), 0)
    sub = replace(
        plan,
        kind="vector",
        expr=X.Load(0),
        accesses=[read],
        reduce_ranges=(),
        reductions={},
    )
    runs = _bare(dev, sub, {**bufs, key: t})
    assert runs is not None  # an f16 output: `_gather` takes any read
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


def _table_only(plan: Plan) -> list:
    """Kept ranges only folded tables read (a stride's phase: which taps of
    a window an output digit takes)."""
    buf = {r for a in plan.accesses if a.slot is not None for r, c in a.coef if c}
    tab = {r for a in plan.accesses if a.slot is None for r, c in a.coef if c}
    return [r for r in plan.out_ranges if U.extent(r) > 1 and r in tab and r not in buf]


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


def _out_access(plan: Plan) -> Access:
    return Access(plan.out_slot, plan.out_dtype, tuple(_out_coef(plan).items()), 0)


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


# --------------------------------------------------------------- matmul
def _ks(k) -> tuple:
    """A matmul's K: one range or several (laid out as one)."""
    return tuple(k) if isinstance(k, tuple) else (k,)


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
                t = Storage(dev, max(32, 2 * n_el))
                tmps.append(t)
                bufs[key] = t
                # The product quantises f16: the layout is f16 whatever acc's type.
                sub = _laid_plan(plan, key, acc, order)
                runs += _vector(dev, replace(sub, name=f"{plan.name}_{side}"), bufs)
            new = Access(key, dtypes.half, tuple(_dense(order).items()), 0)
            if side == "a":
                a = new
            else:
                b = new
        return runs + _product(dev, replace(plan, a=a, b=b), bufs)
    finally:
        for t in tmps:
            t.free()


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


def _pending(dev, plan: Plan, acc: Access, order, bufs, side) -> FM.Pending | None:
    """`acc` as f16 dense over the ranges `order`, for a packing alone to
    read: a `forms.Pending` of one relayout step from the buffer's f16
    values (made inside the packing's kernel), None for a read that is not
    a buffer's float values (a table, a gated read)."""
    if (
        not (FM.PREPARED and FM.PREPARED_QUANT)
        or acc.slot is None
        or acc.gate
        or not dtypes.is_float(acc.dtype)
    ):
        return None
    # Not coalesced: each destination level a row's, a row's or a block's
    # (the packing remaps them, `forms._into_packing`).
    one = _laid_plan(plan, ("pend", id(acc)), acc, order)
    a = one.accesses[0]
    live = [r for r in one.out_ranges if U.extent(r) > 1]
    st, off = FM.base_of(bufs[a.slot])
    dt = F.np_dtype(a.dtype)
    first = a.off + off // np.dtype(dt).itemsize
    if first < 0:
        return None
    coef, dense = dict(a.coef), _dense(live)
    walk = tuple((U.extent(r), coef.get(r, 0)) for r in live)
    dwalk = tuple((U.extent(r), dense[r]) for r in live)
    try:
        src, N, walk, first, dwalk = _f16_walk(dev, st, dt, walk, first, dwalk)
    except FM.FormError:
        return None
    n = _size(live)
    steps = (FM.Step(src, N, walk, first, dwalk, 0, n),)
    mm = _packing_plan(dev, plan)
    rows = mm.m if side == "a" else U.extent(plan.mnk[1])
    rp = mm.mp if side == "a" else mm.n
    group = mm.tm // 4 if side == "a" else mm.tn // 4
    batches = mm.bt if side == "a" or not mm.shared else 1
    if (
        FM.costs.prepared_quant(
            steps,
            Operand("full", pitch=mm.k),
            rows,
            mm.k,
            rp,
            mm.kp,
            side,
            group,
            mm.nk,
            batches,
            dev.machine.board,
        )
        is None
    ):
        return None
    return FM.Pending(dev, 2 * n, steps)


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


def _packing_plan(dev, plan: Plan):
    """One tiling decision shared by deferred-read admission and execution."""
    m, n, k = plan.mnk
    batch = [r for r in plan.out_ranges if r in plan.batch]
    bt = _size(batch)
    machine = MF.load(dev.machine.board)
    block = machine.vc.vlmax // machine.mg.lanes
    Np = -(-U.extent(n) // block) * block
    reads = [classify(acc, list(plan.out_ranges[:-1]), [n]) for acc in plan.accesses]
    quantum = (
        V.STAT_ROWS if any(o is not None and o.kind == "row" for o in reads) else 4
    )
    return MM.plan(
        bt,
        U.extent(m),
        Np,
        _size(_ks(k)),
        not any(plan.b.c(r) for r in batch),
        quantum,
        machine_json=dev.machine.machine_json,
    )


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


def _rows_form(bufs, acc, o: Operand, N: int, Np: int) -> tuple | None:
    """``(address, operand)`` of a product's full read of whole logical rows
    of a buffer held as padded rows (`forms.padded`) at least `Np` wide and
    whole words: those rows as they are (no compaction, no re-padding)."""
    if o.kind != "full" or acc.slot is None:
        return None
    st, off = FM.base_of(bufs[acc.slot])
    key = FM.padded(st)
    if key is None or off:
        return None
    _, n, np_ = key
    start = o.offset // max(n, 1) * np_ + o.offset % max(n, 1)
    if o.pitch != n or o.offset % n + N > n or np_ < Np or np_ % 16 or start % 16:
        return None
    return st.forms[key].addr, replace(o, offset=start, pitch=np_)


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


def _pad_rows(dev, plan: Plan, bufs, acc, batch, m, cols, mp: int, width: int):
    """``(storage, runs)``: `acc` over ``[batch.., m, cols..]`` as f16 with
    each batch's rows padded to `mp`, rows `width` elements apart."""
    order = [*batch, m, *cols]
    strides = [width, *([1] if cols else [])]
    step = mp * width
    for r in reversed(batch):
        strides.insert(0, step)
        step *= U.extent(r)
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
        return t, _vector(
            dev, _laid_plan(plan, key, acc, order, strides), {**bufs, key: t}
        )
    except BaseException:
        t.free()
        raise


def _product_run(
    dev, plan, bufs, mm, binds, extras, bt, M, N, Np, batch, m, n, f16
) -> list:
    # A batch's rows padded to mp (`MM.plan`): the product's rows are moved
    # out of an [bt, mp, Np] temporary.
    mp = mm.mp
    padded = Np != N or mp != M
    tmp: Storage | None = None
    dst, doff = FM.base_of(bufs[plan.out_slot])
    # Rows of N not whole words: the product stays in the output's padded
    # rows form (read back compacted; readers on card take its strides).
    whole = not doff and plan.out_strides is None
    narrow = (2 * N) % 32 != 0 or plan.out_dtype != dtypes.half
    in_form = Np != N and narrow and whole and dtypes.is_float(plan.out_dtype)
    if padded:
        if in_form and (
            mp == M or (bt == 1 and elide_rule("product_rows") and RW.MUTANT is None)
        ):
            out_Rows = mp if (mp != M and bt == 1 and elide_rule("product_rows")) else M
            out_at, key = (
                dst.form(FM.rows_key(N, Np), 2 * bt * out_Rows * Np).addr,
                None,
            )
        elif (
            elide_rule("product_flat")
            and bt == 1
            and Np == N
            and whole
            and dtypes.is_float(plan.out_dtype)
            and plan.out_n * np.dtype(F.np_dtype(plan.out_dtype)).itemsize == dst.nbytes
            and RW.MUTANT is None
        ):
            need_bytes = 2 * bt * mp * Np
            if np.dtype(F.np_dtype(plan.out_dtype)) == np.float16:
                dst.ensure_flat_bytes(need_bytes)
                out_at, key = dst.addr, FLAT
            else:
                out_at, key = (
                    dst.form(FM.F16, max(2 * plan.out_n, need_bytes)).addr,
                    FM.F16,
                )
            padded = False
        else:
            tmp = Storage(dev, 2 * bt * mp * Np)
            out_at, key = tmp.addr, None
    else:
        out_at, key = _out(dev, plan, bufs)
    binds["out"] = out_at
    try:
        arguments = {
            "mm": {
                "made_by": {
                    "gen": "matmul.plan",
                    "args": {
                        "bt": mm.bt,
                        "m": mm.m,
                        "n": mm.n,
                        "k": mm.k,
                        "shared": mm.shared,
                        "quantum": mm.quantum,
                        **MM.pairing_arg(),
                        **MM.plan_knobs(),
                    },
                }
            },
            "e": FM.generated.expr(plan.expr),
            "extras": [vars(o) for o in extras],
            "out": vars(Operand("full", pitch=Np, rows=bt * mp)),
            **MM.kernel_knobs(),
            # A is f16 only where its producer laid it out for the kernel.
            "activation_fp16": f16,
        }
        pending = {int(k[2:]): v for k, v in binds.items() if isinstance(v, FM.Pending)}
        recipe = dict(
            mm=vars(mm),
            e=arguments["e"],
            extras=arguments["extras"],
            out=arguments["out"],
            steps={i: FM.costs.steps_recipe(v.steps) for i, v in pending.items()},
        )
        # The prepared product is the DRAM product: a kernel of no ring.
        depth = MM.resolved(arguments, dev.machine.machine_json)["knobs"]["ring_depth"]
        if (
            pending
            and depth == 0
            and FM.costs.choose("prepared_mm", recipe, dev.machine.board)
        ):
            plans = {i: FM.plans_of(dev, v.steps) for i, v in pending.items()}
            arguments["made"] = {
                str(i): [s.made_by() for s in v.steps] for i, v in pending.items()
            }
            for i, v in pending.items():
                del binds[f"in{i}"]
                binds[f"p{i}_x"] = v.steps[0].src
                binds.update(FM.step_binds(dev, plans[i], f"p{i}_"))
        else:
            binds = {
                k: v.made().addr if isinstance(v, FM.Pending) else v
                for k, v in binds.items()
            }
        run = FM.generated.launch(
            dev,
            "matmul.mm_kernel",
            arguments,
            binds,
            kind="matmul",
            info={"plan": plan.name, "mm": mm},
        )
        if not padded:
            _wrote(dev, plan, bufs, key)
            return [run]
        if tmp is None:
            dst.wrote(FM.rows_key(N, Np))
            dst.dtype = F.np_dtype(plan.out_dtype)
            return [run]
        # The [.., M, N] window of the [.., mp, Np] product: into the output's
        # padded rows form, else the output itself (`_moved`: any window).
        cols = n if not in_form else W.ranges(plan, [Np])[0]
        coef, step = {}, 1
        for r, e in reversed(
            list(zip([*batch, m, cols], [*(U.extent(x) for x in batch), mp, Np]))
        ):
            coef[r] = step
            step *= e
        if not in_form:
            tplan, tbufs, finish = _half_target(dev, plan, bufs)
            moved = _moved(dev, tplan, tbufs, tmp, coef)
            finish()
            return [run, *moved]
        tkey, fkey = ("mmpad", id(plan)), ("mmform", id(plan))
        form = dst.form(FM.rows_key(N, Np), 2 * bt * M * Np)
        moves = {
            **bufs,
            tkey: tmp,
            fkey: Storage(dev, form.nbytes, addr=form.addr, owned=False),
        }
        read = Access(tkey, dtypes.half, tuple(coef.items()), 0)
        moved = _bare(dev, _laid_plan(plan, fkey, read, (*batch, m, cols)), moves)
        assert moved is not None  # an f16 output: `_gather` takes any read
        dst.wrote(FM.rows_key(N, Np))
        dst.dtype = F.np_dtype(plan.out_dtype)
        return [run, *moved]
    finally:
        if tmp is not None:
            tmp.free()


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


def _batched(acc, batch, step: int, what: str) -> None:
    """Refuse a batched operand whose batches are not one row-major stack."""
    want = step
    for r in reversed(batch):
        if U.extent(r) > 1 and acc.c(r) != want:
            raise Refused(
                f"{what}'s batches are not one row-major stack of [rows, K] blocks"
            )
        want *= U.extent(r)


def _is_fusable_elem(plan: Plan | None) -> bool:
    if plan is None or plan.kind != "vector" or plan.reduce_ranges:
        return False
    if isinstance(plan.expr, X.Load) or plan.out_strides is not None:
        return False
    if _selectors(plan) or len(plan.accesses) > MAX_READS:
        return False
    return not (_epilogue_contraction(plan) or _contraction(plan) is not None)


def plan_schedule(items: list, held_ids: set | None = None) -> list:
    """Only adjacent independent elementwise members share a launch."""
    if not horizontal_fusion_enabled():
        return [([i], set()) for i in range(len(items))]
    return launch_schedule.groups(
        items, _is_fusable_elem, ignore_hazards=RW.MUTANT == "ignore_alias"
    )


__all__ = [
    "ELIDE_CONTIGUOUS",
    "HORIZONTAL_FUSION",
    "VIEW_FORMS",
    "classify",
    "elide_contiguous_enabled",
    "execute",
    "flush_multi_stage",
    "horizontal_fusion_enabled",
    "knobs",
    "multi_stage_scope",
    "plan_schedule",
    "single_package_enabled",
    "view_forms_enabled",
]
