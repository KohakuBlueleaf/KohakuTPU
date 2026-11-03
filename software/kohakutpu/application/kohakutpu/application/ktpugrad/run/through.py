"""Reads through a bare read.

tinygrad builds a sliding window by repeating its input (`Tensor._pool`), and
the scheduler stores the repeats and the windows as buffers: kernels that only
walk one buffer into another. A read of such a buffer reads its source at an
index affine on the points it reaches, though neither walk shows it. Over a
schedule each read is composed down to the source, checked point for point,
and a kernel whose output no read is left for does not run. A read of what an
elementwise kernel wrote, read nowhere else, takes that kernel's expression
over its own points: the points it never reaches are not computed.
"""

from collections import Counter
from dataclasses import dataclass, replace

import numpy as np
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.planning import fold as F
from kohakutpu.application.ktpugrad.planning.lower import Access, Plan
from kohakutpu.application.ktpugrad.run.reduce import MAX_READS, _rebuild
from kohakutpu.application.ktpugrad.run.regions import _reads
from kohakutpu.application.runtime import forms as FM
from kohakutpu.language.config import ENV

#: Points a composed read is checked over, at most.
MAX_POINTS = 1 << 22
#: A bare read that several kernels read: "compose" composes each reader
#: through it (each repeats the walk), "once" leaves it to run once, its
#: readers reading its contiguous output.
SHARED = ENV.get("KTPU_THROUGH_SHARED", "compose")
#: Plans by a kernel's plan and what it reads through; a key's plans stay held
#: with its value, so no `id` is reused.
_PLANS: dict = {}


@dataclass(frozen=True)
class _Walk:
    """A buffer a kernel made: its `plan` over `buffers`, the schedule's
    kernels that made it, `sources` the storages it reads."""

    plan: Plan
    buffers: dict
    made: tuple
    sources: tuple


def _elementwise(plan: Plan, buffers: dict) -> bool:
    """Whether `plan` computes each element of a whole buffer on its own."""
    if (
        plan.kind != "vector"
        or plan.reduce_ranges
        or plan.out_strides is not None
        or X.reduces(plan.expr)
    ):
        return False
    for a in plan.accesses:
        if a.gate or a.slot == plan.out_slot:
            return False
        if a.slot is not None and a.slot not in buffers:
            return False
    st, off = FM.base_of(buffers[plan.out_slot])
    item = np.dtype(F.np_dtype(plan.out_dtype)).itemsize
    return not off and plan.out_n * item == st.nbytes


def _bare(plan: Plan, buffers: dict) -> bool:
    """Whether `plan` only walks a buffer of its element type over a whole one."""
    if not isinstance(plan.expr, X.Load) or len(plan.accesses) != 1:
        return False
    a = plan.accesses[0]
    if a.slot is None or a.dtype != plan.out_dtype:
        return False
    return _elementwise(plan, buffers)


def _elements(a: Access, ranges: list) -> np.ndarray:
    """The element `a` reads at every point of `ranges`, one axis each."""
    shape = [U.extent(r) for r in ranges]
    out = np.full(shape, a.off, np.int64)
    for i, r in enumerate(ranges):
        axis = [1] * len(shape)
        axis[i] = shape[i]
        out = out + a.c(r) * np.arange(shape[i]).reshape(axis)
    return out


def _composed(walk: Plan, a: Access, i: int = 0) -> Access | None:
    """`a`, a read of what elementwise `walk` wrote, as `walk`'s read `i` at
    the points `a` reaches; None when that read is not affine."""
    ranges = [r for r, c in a.coef if c and U.extent(r) > 1]
    if int(np.prod([U.extent(r) for r in ranges])) > MAX_POINTS:
        return None
    read = walk.accesses[i]
    at = _elements(a, ranges)
    if at.min() < 0 or at.max() >= walk.out_n:
        return None
    got = _elements(read, list(walk.out_ranges)).reshape(-1)[at]
    off = int(got.reshape(-1)[0])
    units = np.eye(len(ranges), dtype=np.int64)
    steps = [(r, int(got[tuple(unit)]) - off) for r, unit in zip(ranges, units)]
    coef = tuple((r, c) for r, c in steps if c)
    slot = None if read.slot is None else ("through", a.slot, i)
    out = replace(read, slot=slot, coef=coef, off=off)
    return out if np.array_equal(_elements(out, ranges), got) else None


def _pair(r, radix: int, first: int) -> tuple:
    """Ranges ``hi, lo`` of the digits ``r = hi * radix + lo``, ids from `first`."""
    return (
        UOp.range(U.extent(r) // radix, first, r.arg[1]),
        UOp.range(radix, first + 1, r.arg[1]),
    )


def _in_digits(a: Access, r, digits: tuple, radix: int) -> Access:
    hi, lo = digits
    coef = (
        x
        for q, c in a.coef
        for x in (((hi, c * radix), (lo, c)) if q is r else ((q, c),))
    )
    return replace(a, coef=tuple(coef))


def _digits(plan: Plan, r, radix: int) -> Plan:
    """`plan` with range `r` as two digits (`_pair`)."""
    reads = [a for a in (*plan.accesses, plan.a, plan.b) if a is not None]
    read_ranges = [q for a in reads for q, _ in a.coef]
    named = [*plan.out_ranges, *plan.reduce_ranges, *read_ranges]
    pair = _pair(r, radix, 1 + max(q.arg[0] for q in named))

    def swap(rs: tuple) -> tuple:
        return tuple(x for q in rs for x in (pair if q is r else (q,)))

    def read(a: Access | None) -> Access | None:
        return None if a is None else _in_digits(a, r, pair, radix)

    out_ranges = swap(plan.out_ranges)
    strides = plan.out_strides
    if strides is not None:
        pairs = zip(plan.out_ranges, strides, strict=True)
        strides = tuple(
            x for q, s in pairs for x in ((s * radix, s) if q is r else (s,))
        )
    return replace(
        plan,
        out_ranges=out_ranges,
        out_shape=tuple(U.extent(q) for q in out_ranges),
        out_strides=strides,
        reduce_ranges=swap(plan.reduce_ranges),
        reductions={k: swap(v) for k, v in plan.reductions.items()},
        accesses=[read(a) for a in plan.accesses],
        a=read(plan.a),
        b=read(plan.b),
    )


def _radix(walk: Plan, a: Access, i: int = 0) -> tuple | None:
    """``(range, radix)`` in whose digits `a` composes through `walk`'s read
    `i`: a merged range hides axes the walk holds apart."""
    radices = sorted({U.extent(q) for q in walk.out_ranges})
    for r, _ in a.coef:
        for radix in radices:
            if 1 < radix < U.extent(r) and not U.extent(r) % radix:
                digits = _pair(r, radix, 1 << 40)
                if _composed(walk, _in_digits(a, r, digits, radix), i) is not None:
                    return r, radix
    return None


def _rewritten(plan: Plan, walks: dict) -> Plan:
    """`plan` with its reads of the buffers `walks` (by slot) composed."""
    for a in plan.accesses if plan.kind == "vector" else ():
        walk = walks.get(a.slot)
        if walk is not None and not a.gate and _composed(walk, a) is None:
            digit = _radix(walk, a)
            if digit is not None:
                return _rewritten(_digits(plan, *digit), walks)

    def one(a: Access | None) -> Access | None:
        if a is None or a.gate or a.slot not in walks:
            return a
        return _composed(walks[a.slot], a) or a

    accesses = [one(a) for a in plan.accesses]
    a, b = one(plan.a), one(plan.b)
    pairs = [*zip(accesses, plan.accesses), (a, plan.a), (b, plan.b)]
    if all(x is y for x, y in pairs):
        return plan
    ast = (plan.ast, *(w.ast for w in walks.values()))
    return replace(plan, ast=ast, accesses=accesses, a=a, b=b)


def _inlined(plan: Plan, walk: Plan, at: int) -> Plan | None:
    """`plan` with read `at`, of what elementwise `walk` wrote, as `walk`'s
    expression over `plan`'s points; None when a read of it is not affine."""
    a = plan.accesses[at]
    reads = [_composed(walk, a, i) for i in range(len(walk.accesses))]
    if None in reads:
        digit = _radix(walk, a, reads.index(None))
        return None if digit is None else _inlined(_digits(plan, *digit), walk, at)
    base = len(plan.accesses) - 1
    inner = _rebuild(
        walk.expr,
        lambda x, _: (
            X.Load(x.i + base if x.i else at) if isinstance(x, X.Load) else None
        ),
    )
    reductions = {}

    def swap(x, rebuild):
        if isinstance(x, X.Load) and x.i == at:
            return inner
        if isinstance(x, X.Reduce):
            new = X.Reduce(x.op, rebuild(x.arg))
            reductions[id(new)] = plan.reductions[id(x)]
            return new
        return None

    accesses = list(plan.accesses)
    accesses[at] = reads[0]
    return replace(
        plan,
        ast=(plan.ast, walk.ast),
        expr=_rebuild(plan.expr, swap),
        accesses=[*accesses, *reads[1:]],
        reductions=reductions,
    )


def _nest(plan: Plan) -> int:
    """The points `plan`'s expression is computed at."""
    return plan.out_n * int(np.prod([U.extent(r) for r in plan.reduce_ranges]))


def _fold(plan: Plan, buffers: dict, makers: dict, once: set, out: int) -> tuple | None:
    """``(plan, buffers, kernels read through)``: `plan` with its first read
    of a buffer only it reads and `makers` (by storage) made as that kernel's
    expression; None when no read goes."""
    for at, a in enumerate(plan.accesses if plan.kind == "vector" else ()):
        if a.gate or a.slot not in buffers or a.slot == plan.out_slot:
            continue
        st, off = FM.base_of(buffers[a.slot])
        walk = makers.get(id(st))
        if walk is None or off or id(st) not in once or out in walk.sources:
            continue
        reads = len(plan.accesses) + len(walk.plan.accesses) - 1
        if reads > MAX_READS or _nest(plan) > walk.plan.out_n:
            continue
        if a.dtype != walk.plan.out_dtype:
            continue
        key = (id(plan), at, id(walk.plan))
        if key not in _PLANS:
            _PLANS[key] = (plan, walk.plan, _inlined(plan, walk.plan, at))
        new = _PLANS[key][2]
        if new is None:
            continue
        kept = {x.slot for x in new.accesses}
        out_buffers = {s: b for s, b in buffers.items() if s != a.slot or s in kept}
        for i, read in enumerate(walk.plan.accesses):
            if read.slot is not None:
                out_buffers["through", a.slot, i] = walk.buffers[read.slot]
        return new, out_buffers, walk.made
    return None


def _read_through(plan: Plan, buffers: dict, walks: dict, out: int, once: set) -> tuple:
    """``(plan, buffers, kernels read through)``: the reads of `plan` of the
    buffers `walks` (by storage) composed, none onto the storage it writes,
    and with `SHARED` "once" none of a buffer more than one kernel reads."""
    hits = {}
    for slot, buf in buffers.items():
        st, off = FM.base_of(buf)
        walk = walks.get(id(st))
        stale = walk is None or out in walk.sources
        shared = SHARED == "once" and id(st) not in once
        if not stale and not shared and not off and slot != plan.out_slot:
            hits[slot] = walk
    if not hits:
        return plan, buffers, ()
    key = (id(plan), tuple((s, id(w.plan)) for s, w in hits.items()))
    if key not in _PLANS:
        plans = {s: w.plan for s, w in hits.items()}
        _PLANS[key] = (plan, plans, _rewritten(plan, plans))
    new = _PLANS[key][2]
    reads = [a.slot for a in (*new.accesses, new.a, new.b) if a is not None]
    out_buffers = dict(buffers)
    made: tuple = ()
    for slot, walk in hits.items():
        if ("through", slot, 0) in reads:
            source = walk.plan.accesses[0].slot
            out_buffers[("through", slot, 0)] = walk.buffers[source]
            made += walk.made
            if slot not in reads:
                del out_buffers[slot]
    return new, out_buffers, made


def _unwritten(walks: dict, out: int) -> dict:
    """`walks` (by storage) but those a write of `out` makes stale."""
    return {st: w for st, w in walks.items() if out != st and out not in w.sources}


def _once(items: list) -> set:
    """The storages one kernel of `items` reads, a bare read of one counting
    as each reader of what it wrote."""
    readers = Counter(st for plan, buffers in items for st in _reads(plan, buffers))
    for plan, buffers in reversed(items):
        if _bare(plan, buffers):
            source = FM.base_of(buffers[plan.accesses[0].slot])[0]
            out = FM.base_of(buffers[plan.out_slot])[0]
            readers[id(source)] += max(readers[id(out)], 1) - 1
    return {st for st, n in readers.items() if n == 1}


def through(items: list, held: set) -> tuple:
    """``(items, owners)``: `items` (`(plan, buffers)`) with reads composed
    through bare reads and into their elementwise makers, a kernel whose output
    no read is left for and no tensor names (`held`, by storage) gone, and each
    kernel left's schedule indices: its own and those it read through, what it
    now reads living until it has run."""
    once = _once(items)
    walks: dict = {}
    makers: dict = {}
    composed, owners, through_of = [], [], set()
    for i, (plan, buffers) in enumerate(items):
        out = id(FM.base_of(buffers[plan.out_slot])[0])
        plan, buffers, made = _read_through(plan, buffers, walks, out, once)
        while folded := _fold(plan, buffers, makers, once, out):
            plan, buffers = folded[:2]
            made += folded[2]
        composed.append((plan, buffers))
        owners.append(sorted({*made, i}))
        through_of.update(made)
        walks = _unwritten(walks, out)
        makers = _unwritten(makers, out)
        if _elementwise(plan, buffers):
            slots = [a.slot for a in plan.accesses if a.slot is not None]
            sources = tuple(id(FM.base_of(buffers[s])[0]) for s in slots)
            walk = _Walk(plan, buffers, (i, *made), sources)
            if _bare(plan, buffers):
                walks[out] = walk
            elif out not in held:
                makers[out] = walk
    reads: set = set()
    left = []
    for i in reversed(range(len(composed))):
        plan, buffers = composed[i]
        out = id(FM.base_of(buffers[plan.out_slot])[0])
        if i not in through_of or out in held or out in reads:
            reads |= _reads(plan, buffers)
            left.insert(0, i)
    return [composed[i] for i in left], [owners[i] for i in left]
