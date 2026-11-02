"""Row regions: adjacent kernels over one row's values, as one kernel.

A reduction ends a kernel, so a normalisation or a softmax schedules as
statistic, elementwise, statistic, elementwise, each reading and writing whole
buffers. Where the intermediates are read nowhere else, one rows kernel holds
the row in registers: the sources read once, the result written once
(`rows.run`, which splits a row wider than a column block again).
"""

from dataclasses import dataclass, replace

from tinygrad.dtype import dtypes

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.planning.lower import Plan
from kohakutpu.application.ktpugrad.run.common import _live, _mult
from kohakutpu.application.ktpugrad.run.reduce import (
    MAX_READS,
    _all_reduces,
    _rebuild,
    _selectors,
    _vector_choice,
)
from kohakutpu.application.runtime import forms as FM

#: Region plans by their members and which buffers those share.
_PLANS: dict = {}


@dataclass(frozen=True)
class _Made:
    """A member's output: `expr` over `accesses`, kept over `ranges`, the
    row's values `col`."""

    expr: object
    accesses: tuple
    ranges: tuple
    col: object


class _NoRegion(Exception):
    """The members do not compose."""


def _col(plan: Plan):
    """The range of a row's values: reduced, else the innermost kept."""
    return plan.reduce_ranges[0] if plan.reduce_ranges else _live(plan.out_ranges)[-1]


def _member(plan: Plan) -> bool:
    return (
        plan.kind == "vector"
        and plan.out_dtype == dtypes.half
        and plan.out_strides is None
        and not isinstance(plan.expr, X.Load)
        and len(plan.reduce_ranges) <= 1
        and bool(_live(plan.out_ranges))
        and not _selectors(plan)
        and _vector_choice(plan)[0] == "general"
    )


def _renames(acc, made: _Made, col) -> dict:
    """The ranges of `made` as `acc`, a whole-buffer read of it, walks them."""
    live = _live(made.ranges)
    coef = {r: c for r, c in acc.coef if U.extent(r) > 1}
    names = {made.col: col}
    if acc.off or acc.gate or U.extent(made.col) != U.extent(col):
        raise _NoRegion
    for r, stride in zip(live, _mult(live)):
        to = [q for q, c in coef.items() if c == stride and U.extent(q) == U.extent(r)]
        if not to or (r == made.col) != (to[0] == col):
            raise _NoRegion
        names[r] = to[0]
    if set(coef) - set(names.values()):
        raise _NoRegion
    return names


def _absorb(plan: Plan, ids: dict, made: dict) -> _Made:
    """`plan` with the outputs of `made` (by buffer) read as their expressions."""
    col, accesses, shared = _col(plan), [], {}

    def f(x, walk):
        if not isinstance(x, X.Load):
            return None
        a = plan.accesses[x.i]
        held = made.get(ids.get(a.slot))
        if held is None:
            accesses.append(replace(a, slot=ids.get(a.slot)))
            return X.Load(len(accesses) - 1)
        names = _renames(a, held, col)
        key = (id(held), frozenset(names.items()))
        if key not in shared:
            base = len(accesses)
            accesses.extend(
                replace(r, coef=tuple((names.get(q, q), c) for q, c in r.coef))
                for r in held.accesses
            )
            shared[key] = _rebuild(
                held.expr,
                lambda y, _: X.Load(base + y.i) if isinstance(y, X.Load) else None,
            )
        return shared[key]

    expr = _rebuild(plan.expr, f)
    return _Made(expr, tuple(accesses), plan.out_ranges, col)


def _one_read_each(made: _Made) -> tuple:
    """`(expression, accesses)` with equal reads one access, and what members
    computed alike (a statistic each reads, a shared exponential) one node."""
    seen: dict = {}
    accesses: list = []

    def f(x, walk):
        if not isinstance(x, X.Load):
            return None
        a = made.accesses[x.i]
        key = (a.slot, a.dtype, frozenset(a.coef), a.off, id(a.table))
        if key not in seen:
            seen[key] = len(accesses)
            slot = None if a.slot is None else ("region", a.slot)
            accesses.append(replace(a, slot=slot))
        return X.Load(seen[key])

    return X.intern(_rebuild(made.expr, f)), accesses


def _plan(last: Plan, members: tuple, made: _Made, out: int) -> Plan | None:
    """The region's plan, None when no kernel holds it."""
    expr, accesses = _one_read_each(made)
    col = made.col
    if len(accesses) > MAX_READS or not _all_reduces(expr):
        return None
    if any(a.slot is not None and a.c(col) not in (0, 1) for a in accesses):
        return None
    return replace(
        last,
        ast=tuple(p.ast for p in members),
        name=f"{last.name}_region",
        out_slot=("region", out),
        expr=expr,
        accesses=accesses,
        reduce_ranges=(),
        reductions={},
    )


def _identity(buffer) -> tuple:
    """A buffer's logical bytes: a whole view of a storage is that storage."""
    st, off = FM.base_of(buffer)
    return id(st), off, buffer.nbytes


def _ids(plan: Plan, buffers: dict, known: dict) -> dict:
    """Each slot of `plan` as the region's ordinal of its bytes."""
    return {s: known.setdefault(_identity(b), len(known)) for s, b in buffers.items()}


def _region(members: list) -> tuple | None:
    """`(plan, buffers)` of `members` (`(plan, buffers)` pairs), None when
    they do not compose. Plans are kept by their members and shared buffers."""
    known: dict = {}
    ids = [_ids(p, b, known) for p, b in members]
    key = tuple(
        (id(p), tuple(sorted(i.items(), key=repr)))
        for (p, _), i in zip(members, ids, strict=True)
    )
    if key not in _PLANS:
        _PLANS[key] = _compose(members, ids)
    region = _PLANS[key]
    if region is None:
        return None
    every = {
        ("region", known[_identity(b)]): b for _, bs in members for b in bs.values()
    }
    used = {a.slot for a in region.accesses if a.slot is not None}
    # A member's output is gone once the region runs: nothing may still read it.
    gone = {id(FM.base_of(b[p.out_slot])[0]) for p, b in members[:-1]}
    if any(id(FM.base_of(every[s])[0]) in gone for s in used):
        return None
    return region, {k: every[k] for k in {*used, region.out_slot}}


def _compose(members: list, ids: list) -> Plan | None:
    last = members[-1][0]
    if last.reduce_ranges:
        return None
    made: dict = {}
    try:
        for (plan, _), slots in zip(members, ids, strict=True):
            made[slots[plan.out_slot]] = _absorb(plan, slots, made)
    except _NoRegion:
        return None
    out = ids[-1][last.out_slot]
    return _plan(last, tuple(p for p, _ in members), made[out], out)


def _reads(plan: Plan, buffers: dict) -> set:
    """Every buffer `plan` names but its output, and the output where it reads
    it: a product reads through `plan.a` and `plan.b`, not `accesses`."""
    named = {s for s in buffers if s != plan.out_slot}
    named |= {a.slot for a in plan.accesses if a.slot in buffers}
    return {id(FM.base_of(buffers[s])[0]) for s in named}


def regions(items: list, held: set) -> list:
    """`items` (`(plan, buffers)`) as `(indices, plan, buffers)`: a region one
    entry of its members' indices. A member's output must be whole, read only
    inside the region and named by no tensor (`held`, by storage)."""
    reads = [_reads(p, b) for p, b in items]
    out, i = [], 0
    while i < len(items):
        best = None
        k = i
        while k < len(items) and _member(items[k][0]):
            if k > i and _closes(items, reads, held, i, k):
                region = _region(items[i : k + 1])
                if region is not None:
                    best = (k, *region)
            k += 1
        if best is None:
            out.append(([i], *items[i]))
            i += 1
        else:
            k, plan, buffers = best
            out.append((list(range(i, k + 1)), plan, buffers))
            i = k + 1
    return out


def _closes(items, reads, held, i: int, k: int) -> bool:
    """Whether members `i..k` hide every output but the last, and write
    nothing they also read as a source."""
    outs = [FM.base_of(p[1][p[0].out_slot]) for p in items[i : k + 1]]
    if any(off for _, off in outs):
        return False
    made = [id(st) for st, _ in outs]
    if len(set(made)) != len(made):
        return False
    inside = set().union(*reads[i : k + 1])
    if made[-1] in inside:
        return False
    for at, st in enumerate(made[:-1], start=i):
        if st in held or not any(st in r for r in reads[at + 1 : k + 1]):
            return False
        if any(st in r for r in reads[k + 1 :]):
            return False
    return True
