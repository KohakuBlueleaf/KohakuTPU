"""Reductions and contractions: pruning, fold order, vector choice."""

import itertools
from dataclasses import replace

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.memo import memo
from kohakutpu.application.ktpugrad.planning import lower as L
from kohakutpu.application.ktpugrad.planning import window as W
from kohakutpu.application.ktpugrad.planning.lower import Access, Plan
from kohakutpu.application.ktpugrad.planning.window import tabled
from kohakutpu.application.ktpugrad.run.common import _live
from kohakutpu.application.ktpugrad.run.moves import _coalesce


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
    # A product the outer expression reads as it lies needs no transposed read.
    for order in sorted(orders, key=lambda o: [rank.get(r, len(rank)) for r in o]):
        probe = _coalesce(laid(order))
        if _contraction(probe) is not None:
            return probe, list(order), True
    return laid(outside), outside, False


def _epilogue_contraction(plan: Plan) -> bool:
    """A product under elementwise math the planner did not see as one (its
    output permuted, a bias beside it): the clusters take its leaf."""
    if not plan.reduce_ranges or isinstance(plan.expr, X.Reduce):
        return False
    if len(_all_reduces(plan.expr)) != 1:
        return False
    return _leaf(plan, _leaf_reduce(plan))[2]


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
    wide = W.find(plan, plain=True) if plan.reduce_ranges else None
    if wide is not None:
        return "window", wide
    return "general", None


# ------------------------------------------------------------ rows path
#: What a padding lane holds so a reduction ignores it.
IDENTITY = {"sum": 0.0, "max": -65504.0, "prod": 1.0}


def _table_only(plan: Plan) -> list:
    """Kept ranges only folded tables read (a stride's phase: which taps of
    a window an output digit takes)."""
    buf = {r for a in plan.accesses if a.slot is not None for r, c in a.coef if c}
    tab = {r for a in plan.accesses if a.slot is None for r, c in a.coef if c}
    return [r for r in plan.out_ranges if U.extent(r) > 1 and r in tab and r not in buf]


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
