"""A window contraction: one sum of the product of two reads, either of them
a window (two ranges on one coordinate: ``x[s * oy + d * ky]``) or gated by
a padding (a box of the read's coordinates), as the clusters take it.

A read's coordinates (`Dim`) are its ranges grouped: ranges at one stride,
and ranges a gate couples (its bound on one, given the other, moves), each
an integer multiple of the group's stride (``2 * oy + ky - 1`` is one
coordinate at stride W of a stride-2 convolution); a gate is a box of them. The
runner lays each side out as ``[outer.., K.., lane]``: the lane a K range
each side reads alone (a channel, the batch), innermost and padded to whole
16-element words, so every other step of the layout is whole words and the
window is one mover walk (`run._window`). Without such a range the lane is
a virtual one of extent 1. M is the side with more outer elements.
"""

import itertools
from dataclasses import dataclass, replace

import numpy as np
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.lower import Access, Plan

#: Lanes of a 32-byte word of f16.
WORD = 16


@dataclass(frozen=True)
class Dim:
    """One coordinate of a read: ``sum(mult * range)`` at `stride`."""

    stride: int
    terms: tuple  # ((range, mult), ...), mult a nonzero integer
    #: valid coordinates ``[a, b)``, counted from the least one
    box: tuple = (0, 0)

    @property
    def lo(self) -> int:
        """The least coordinate."""
        return sum(m * (U.extent(r) - 1) for r, m in self.terms if m < 0)

    @property
    def span(self) -> int:
        return 1 + sum(abs(m) * (U.extent(r) - 1) for r, m in self.terms)

    def ranges(self) -> list:
        return [r for r, _ in self.terms]


@dataclass(frozen=True)
class Side:
    """One operand: its read, its coordinates (largest stride first), the
    lane's coordinate (None: a virtual lane of one)."""

    acc: Access
    dims: tuple
    lane: Dim | None

    def dim(self, r) -> Dim:
        return next(d for d in self.dims if r in d.ranges())

    def mult(self, r) -> int:
        return dict(self.dim(r).terms)[r]


@dataclass
class Contraction:
    plan: Plan  # the kernel's plan, reduced ranges a gate bounds trimmed
    red: X.Reduce
    scale: float
    a: Side  # M rows
    b: Side  # N columns
    bt: tuple
    ma: tuple
    nb: tuple
    ks: tuple  # K but the lane, outermost first
    lane: object  # the lane's range, None for a virtual lane
    pad: int  # the lane's extent in whole words


def _live(rs) -> list:
    return [r for r in rs if U.extent(r) > 1]


def _reduces(e, out) -> list:
    if isinstance(e, X.Reduce):
        if any(r is e for r in out):  # a node the expression shares
            return out
        out.append(e)
        _reduces(e.arg, out)
    elif isinstance(e, X.Op):
        for a in e.args:
            _reduces(a, out)
    return out


def _table(acc: Access) -> tuple:
    """``(ranges outermost first, the table over them)``."""
    rs = [r for r, _ in sorted(acc.coef, key=lambda rc: -rc[1])]
    return rs, np.asarray(acc.table).reshape([U.extent(r) for r in rs])


def dims(acc: Access, coupled: tuple = ()) -> tuple:
    """`acc`'s coordinates, largest stride first: its ranges at one stride
    grouped, and those of each set in `coupled` (merging groups)."""
    coef = {r: c for r, c in acc.coef if c and U.extent(r) > 1}
    by: dict = {}
    for r, c in coef.items():
        by.setdefault(abs(c), set()).add(r)
    groups = list(by.values())
    for s in coupled:
        hit = [g for g in groups if g & s]
        groups = [g for g in groups if not g & s]
        if hit:
            groups.append(set().union(*hit))
    out = []
    for g in groups:
        stride = int(np.gcd.reduce([abs(coef[r]) for r in g]))
        terms = tuple((r, coef[r] // stride) for r, _ in acc.coef if r in g)
        out.append(Dim(stride, terms))
    out.sort(key=lambda d: -d.stride)
    return tuple(replace(d, box=(0, d.span)) for d in out)


def coupled(rs: list, g) -> tuple:
    """The sets of `rs` the gate `g` (a table over them) couples: two ranges
    are when g's projection on the pair is not the product of its
    projections on each."""
    on = [r for r in rs if U.extent(r) > 1]
    parent = {r: r for r in on}

    def root(r):
        while parent[r] is not r:
            r = parent[r]
        return r

    for i, j in itertools.combinations(range(len(rs)), 2):
        if rs[i] not in parent or rs[j] not in parent:
            continue
        rest = tuple(k for k in range(len(rs)) if k not in (i, j))
        pair = g.any(axis=rest) if rest else g
        if not np.array_equal(pair, np.outer(pair.any(1), pair.any(0))):
            parent[root(rs[i])] = root(rs[j])
    sets: dict = {}
    for r in on:
        sets.setdefault(root(r), set()).add(r)
    return tuple(s for s in sets.values() if len(s) > 1)


def windowed(acc: Access) -> bool:
    return any(len(d.terms) > 1 for d in dims(acc))


def _factors(e, accesses) -> tuple | None:
    """``(buffer reads, gate tables, scale)`` of a product of reads,
    constants and zero-else `where`s on tables; None otherwise."""
    loads, gates, scale = [], [], 1.0

    def walk(x) -> bool:
        nonlocal scale
        if isinstance(x, X.Load):
            if accesses[x.i].slot is None:
                return False
            loads.append(x.i)
            return True
        if isinstance(x, X.Const):
            scale *= x.value
            return True
        if isinstance(x, X.Op) and x.name == "mul":
            return all(walk(a) for a in x.args)
        if (
            isinstance(x, X.Op)
            and x.name == "where"
            and isinstance(x.args[0], X.Load)
            and accesses[x.args[0].i].slot is None
            and x.args[2] == X.Const(0.0)
        ):
            gates.append(x.args[0].i)
            return walk(x.args[1])
        return False

    return (loads, gates, scale) if walk(e) else None


def _trim(plan: Plan, gates: list, reduced: list) -> Plan:
    """Each reduced range a gate holds false outside one interval of
    narrowed to it (the sum skips what it adds zero for)."""
    cut: dict = {}
    for i in gates:
        rs, arr = _table(plan.accesses[i])
        for k, r in enumerate(rs):
            if r not in reduced:
                continue
            on = np.flatnonzero(
                arr.any(axis=tuple(j for j in range(arr.ndim) if j != k))
            )
            if not len(on) or on[-1] - on[0] + 1 != len(on):
                continue
            a, b = cut.get(r, (0, U.extent(r)))
            cut[r] = (max(a, int(on[0])), min(b, int(on[-1]) + 1))
    cut = {r: ab for r, ab in cut.items() if ab != (0, U.extent(r)) and ab[0] < ab[1]}
    if not cut:
        return plan
    new = {
        r: r.replace(src=(r.src[0].replace(arg=b - a), *r.src[1:]))
        for r, (a, b) in cut.items()
    }
    accesses = []
    for acc in plan.accesses:
        off = acc.off + sum(c * cut[r][0] for r, c in acc.coef if r in cut)
        coef = tuple((new.get(r, r), c) for r, c in acc.coef)
        table = acc.table
        if acc.slot is None and any(r in cut for r, _ in acc.coef):
            rs, arr = _table(acc)
            arr = np.ascontiguousarray(
                arr[tuple(slice(*cut[r]) if r in cut else slice(None) for r in rs)]
            )
            keep = [new.get(r, r) for r in rs]
            step, cf = 1, {}
            for r in reversed(keep):
                cf[r] = step
                step *= U.extent(r)
            coef, table, off = tuple((r, cf[r]) for r in keep), arr.reshape(-1), 0
        accesses.append(replace(acc, off=off, coef=coef, table=table))
    return replace(
        plan,
        accesses=accesses,
        reduce_ranges=tuple(new.get(r, r) for r in plan.reduce_ranges),
        reductions={
            k: tuple(new.get(r, r) for r in v) for k, v in plan.reductions.items()
        },
    )


def gate_of(plan: Plan, gates: list):
    """``(ranges, the conjunction of the gate tables over them)``."""
    rs: list = []
    for i in gates:
        for r in _table(plan.accesses[i])[0]:
            if r not in rs:
                rs.append(r)
    g = np.ones([U.extent(r) for r in rs], bool)
    for i in gates:
        own, arr = _table(plan.accesses[i])
        order = [own.index(r) for r in rs if r in own]
        arr = np.transpose(arr != 0, order)
        shape = [U.extent(r) if r in own else 1 for r in rs]
        g &= arr.reshape(shape)
    return rs, g


def boxed(side: Side, rs: list, g, exact: bool = True) -> Side | None:
    """`side` with the gate `g` over `rs` as a box of its coordinates, or
    None when it is not one; not `exact`, each coordinate's box bounds where
    the gate holds (it stays a select)."""
    mine = {r for d in side.dims for r in d.ranges()}
    if any(r not in mine for r in rs if U.extent(r) > 1):
        return None
    touched = [d for d in side.dims if any(r in rs for r in d.ranges())]
    if any(r not in rs for d in touched for r in d.ranges()):
        return None
    grids = np.meshgrid(*(np.arange(U.extent(r)) for r in rs), indexing="ij")
    at = dict(zip(rs, grids))
    want = np.ones(g.shape, bool)
    out = []
    for d in side.dims:
        if d not in touched:
            out.append(d)
            continue
        coord = sum(s * at[r] for r, s in d.terms) - d.lo
        on = coord[g]
        if not on.size:
            return None
        a, b = int(on.min()), int(on.max()) + 1
        want &= (coord >= a) & (coord < b)
        out.append(replace(d, box=(a, b)))
    if exact and not np.array_equal(want, g):
        return None
    return replace(side, dims=tuple(out))


def find(plan: Plan) -> Contraction | None:
    """`plan` as a window contraction (see the module), or None."""
    reds = _reduces(plan.expr, [])
    if len(reds) != 1 or reds[0].op != "sum":
        return None
    red = reds[0]
    got = _factors(red.arg, plan.accesses)
    if got is None or len(got[0]) != 2:
        return None
    loads, gates, scale = got
    a0, b0 = (plan.accesses[i] for i in loads)
    if not (gates or windowed(a0) or windowed(b0)):
        return None
    reduced = list(plan.reductions[id(red)])
    plan = _trim(plan, gates, reduced)
    red_ranges = _live(plan.reductions[id(red)])
    a0, b0 = (plan.accesses[i] for i in loads)
    rs, g = gate_of(plan, gates) if gates else ([], None)
    tied = coupled(rs, g) if gates else ()
    sa, sb = (Side(x, dims(x, tied), None) for x in (a0, b0))
    if gates:
        for which in ("a", "b"):
            side = boxed(sa if which == "a" else sb, rs, g)
            if side is not None:
                sa, sb = (side, sb) if which == "a" else (sa, side)
                break
        else:
            return None
    out = _live(plan.out_ranges)
    reads = [{r for d in s.dims for r in d.ranges()} for s in (sa, sb)]
    if any(not (r in reads[0] and r in reads[1]) for r in red_ranges):
        return None
    if any(r not in reads[0] and r not in reads[1] for r in out):
        return None
    if any(r not in out and r not in red_ranges for s in reads for r in s):
        return None
    bt = tuple(r for r in out if r in reads[0] and r in reads[1])
    ma = tuple(r for r in out if r in reads[0] and r not in reads[1])
    nb = tuple(r for r in out if r in reads[1] and r not in reads[0])
    size = lambda rs: int(np.prod([U.extent(r) for r in rs])) if rs else 1
    if size(nb) > size(ma):
        sa, sb, ma, nb = sb, sa, nb, ma

    def plain(s: Side, r) -> bool:
        d = s.dim(r)
        return d.terms == ((r, 1),) and d.box == (0, d.span)

    lanes = [r for r in red_ranges if plain(sa, r) and plain(sb, r)]
    lane = max(
        lanes, key=lambda r: (U.extent(r) % WORD == 0, U.extent(r)), default=None
    )
    pad = -(-U.extent(lane) // WORD) * WORD if lane is not None else WORD
    if lane is not None:
        sa, sb = (replace(s, lane=s.dim(lane)) for s in (sa, sb))
    ks = tuple(
        sorted(
            (r for r in red_ranges if r is not lane),
            key=lambda r: (-sa.dim(r).stride, r.arg[0]),
        )
    )
    return Contraction(plan, red, scale, sa, sb, bt, ma, nb, ks, lane, pad)


def _weights(e, accesses) -> tuple | None:
    """``(buffer reads, [(table read, how)], scale)`` of a product of reads,
    constants and `where`s on tables with a zero arm; `how` is "value" (a
    table read as a factor), "on" (the factor 1 where the table is nonzero)
    or "off" (1 where it is zero). None otherwise."""
    loads, tables, scale = [], [], 1.0

    def walk(x) -> bool:
        nonlocal scale
        if isinstance(x, X.Load):
            if accesses[x.i].slot is None:
                tables.append((x.i, "value"))
            else:
                loads.append(x.i)
            return True
        if isinstance(x, X.Const):
            scale *= x.value
            return True
        if isinstance(x, X.Op) and x.name == "mul":
            return all(walk(a) for a in x.args)
        if (
            isinstance(x, X.Op)
            and x.name == "where"
            and isinstance(x.args[0], X.Load)
            and accesses[x.args[0].i].slot is None
        ):
            if x.args[2] == X.Const(0.0):
                tables.append((x.args[0].i, "on"))
                return walk(x.args[1])
            if x.args[1] == X.Const(0.0):
                tables.append((x.args[0].i, "off"))
                return walk(x.args[2])
        return False

    return (loads, tables, scale) if walk(e) else None


def _over(acc: Access, order: list) -> np.ndarray:
    """`acc`'s table over the ranges `order` (each it does not read
    broadcast)."""
    flat = np.asarray(acc.table, np.float64).reshape(-1)
    idx = np.full([U.extent(r) for r in order], acc.off, np.int64)
    for i, r in enumerate(order):
        shape = [1] * len(order)
        shape[i] = U.extent(r)
        idx = idx + acc.c(r) * np.arange(U.extent(r)).reshape(shape)
    return flat[idx]


def tabled(plan: Plan) -> Plan | None:
    """`plan`'s one sum of a buffer read x times tables (pads, selects,
    folded reads), the tables coupling a kept range with a reduced one (a
    bound that moves: a cumulative sum, a window over a constant), as a sum
    over x's source coordinates j of x[j] times one table over the kept
    ranges and j: ``sum_k T(o, k) x[f(o, k)] = sum_j x[j] T'(o, j)``, T'
    accumulated on the host and trimmed to where it is nonzero (so x is
    read only inside the box the pads allowed). None when it is not one."""
    reds = _reduces(plan.expr, [])
    if len(reds) != 1 or reds[0].op != "sum":
        return None
    red = reds[0]
    got = _weights(red.arg, plan.accesses)
    if got is None or len(got[0]) != 1 or not got[1]:
        return None
    (xi,), tables, scale = got
    x = plan.accesses[xi]
    reduced = _live(plan.reductions[id(red)])
    out = _live(plan.out_ranges)
    loop = [*out, *reduced]
    tread = {
        r for i, _ in tables for r, c in plan.accesses[i].coef if c and U.extent(r) > 1
    }
    xread = {r for r, c in x.coef if c and U.extent(r) > 1}
    if any(r not in loop for r in tread | xread):
        return None
    tab = [r for r in loop if r in tread]
    if not any(r in reduced for r in tab) or not any(r in out for r in tab):
        return None
    t = np.full([U.extent(r) for r in tab], float(scale))
    for i, how in tables:
        v = _over(plan.accesses[i], tab)
        t = t * (v if how == "value" else (v != 0) if how == "on" else (v == 0))
    xs = dims(x, coupled(tab, t != 0))
    js = [d for d in xs if any(r in reduced for r in d.ranges())]
    kept = [r for d in xs if d not in js for r in d.ranges()]
    # The grid: the kept ranges the table or a coordinate reads, then every
    # reduced range (a reduced range no factor reads adds its count).
    o_rs = [r for r in out if r in tab or any(r in d.ranges() for d in js)]
    grid = [*o_rs, *reduced]
    shape = [U.extent(r) for r in grid]
    w = np.broadcast_to(_on(t, tab, grid), shape)
    pos = dict(zip(grid, np.indices(shape, sparse=True)))
    span = [*(U.extent(r) for r in o_rs), *(d.span for d in js)]
    at = [np.broadcast_to(pos[r], shape) for r in o_rs]
    at += [
        np.broadcast_to(sum(m * pos[r] for r, m in d.terms) - d.lo, shape) for d in js
    ]
    flat = np.ravel_multi_index([a.reshape(-1) for a in at], span)
    acc = np.bincount(flat, weights=w.reshape(-1), minlength=int(np.prod(span)))
    acc = acc.reshape(span)
    nz = acc != 0
    if not nz.any():
        return _swapped(plan, red, X.Const(0.0), [], ())
    box = []
    for k in range(len(o_rs), acc.ndim):
        on = np.flatnonzero(nz.any(axis=tuple(a for a in range(acc.ndim) if a != k)))
        box.append((int(on[0]), int(on[-1]) + 1))
    acc = acc[(slice(None),) * len(o_rs) + tuple(slice(a, b) for a, b in box)]
    jr = ranges(plan, [b - a for a, b in box])
    xc = [(r, c) for r, c in x.coef if r in kept]
    xc += [(j, d.stride) for j, d in zip(jr, js)]
    xoff = x.off + sum(d.stride * (d.lo + a) for d, (a, _) in zip(js, box))
    xa = Access(x.slot, x.dtype, tuple(xc), xoff)
    order = [*o_rs, *jr]
    ta = Access(
        None, x.dtype, tuple(zip(order, _dense(order))), 0, table=acc.reshape(-1)
    )
    n = len(plan.accesses)
    new = X.Reduce("sum", X.Op("mul", (X.Load(n), X.Load(n + 1))))
    return _swapped(plan, red, new, [xa, ta], tuple(jr))


def _on(t: np.ndarray, order: list, grid: list) -> np.ndarray:
    """`t` over the ranges `order` with its axes placed on `grid` (a
    superset of them): the others of extent 1."""
    perm = sorted(range(len(order)), key=lambda i: grid.index(order[i]))
    return np.transpose(t, perm).reshape(
        [U.extent(r) if r in order else 1 for r in grid]
    )


def _dense(rs) -> list:
    out, step = [], 1
    for r in reversed(rs):
        out.insert(0, step)
        step *= U.extent(r)
    return out


def _swapped(plan: Plan, red, new, extra: list, rs: tuple) -> Plan:
    """`plan` with the reduction `red` replaced by `new` (a reduction over
    `rs`, or no reduction), reading `extra` after its own accesses, unused
    accesses dropped."""
    used: dict = {}
    reductions: dict = {}
    memo: dict = {}

    def walk(x):
        if id(x) in memo:  # a node the expression shares stays one
            return memo[id(x)]
        if x is red and isinstance(new, X.Reduce):
            got = X.Reduce(new.op, walk(new.arg))
            reductions[id(got)] = rs
        elif x is red:
            got = new
        elif isinstance(x, X.Load):
            got = X.Load(used.setdefault(x.i, len(used)))
        elif isinstance(x, X.Op):
            got = X.Op(x.name, tuple(walk(a) for a in x.args))
        else:
            got = x
        memo[id(x)] = got
        return got

    e = walk(plan.expr)
    accesses = [*plan.accesses, *extra]
    keep = [accesses[i] for i, _ in sorted(used.items(), key=lambda kv: kv[1])]
    return replace(plan, expr=e, accesses=keep, reduce_ranges=rs, reductions=reductions)


def ranges(plan: Plan, extents: list) -> list:
    """New loop ranges of `extents`, ids no range of `plan` has, of the
    kind of its output's."""
    seen = [r.arg[0] for a in plan.accesses for r, _ in a.coef]
    seen += [r.arg[0] for r in (*plan.out_ranges, *plan.reduce_ranges)]
    top = 1 + max(seen, default=0)
    kind = plan.out_ranges[0].arg[1]
    return [UOp.range(n, top + i, kind) for i, n in enumerate(extents)]


__all__ = [
    "WORD",
    "Contraction",
    "Dim",
    "Side",
    "boxed",
    "coupled",
    "dims",
    "find",
    "gate_of",
    "ranges",
    "tabled",
    "windowed",
]
