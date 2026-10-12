"""Reading shape and stride out of a tinygrad kernel ast (0.14).

A kernel arrives as a loop nest: ``SINK(END(STORE(INDEX(param, idx),
value), *ranges))`` (``SINK(STORE(...))`` with no loop), buffers as `PARAM`,
a read as ``INDEX(param, idx)``, `idx` an integer expression over `RANGE`s.
`affine` reads `idx` as ``offset + sum(coefficient * range)`` -- the stride
row of the access -- and refuses anything else (a division, a modulo), since
a stride guessed wrong is a wrong address.
"""

from tinygrad.uop import Ops
from tinygrad.uop.ops import AxisType


def envelope(ast):
    """``(store, ranges)`` of a kernel ast, or None."""
    if ast.op is not Ops.SINK or len(ast.src) != 1:
        return None
    top = ast.src[0]
    if top.op is Ops.STORE:
        return top, ()
    if top.op is not Ops.END or not top.src:
        return None
    store = top.src[0]
    if store.op is not Ops.STORE:
        return None
    ranges = tuple(top.src[1:])
    if any(counted(r) is None for r in ranges):
        return None
    return store, ranges


def counted(rng) -> int | None:
    """How many steps a RANGE takes, or None when not a constant."""
    if rng.op is not Ops.RANGE or not rng.src:
        return None
    n = rng.src[0]
    return n.arg if n.op is Ops.CONST and type(n.arg) is int else None


def extent(rng) -> int:
    """How many steps a range of an accepted envelope takes (a constant)."""
    n = counted(rng)
    if n is None:
        raise ValueError(f"a range of no constant extent: {rng}")
    return n


def reduces(rng) -> bool:
    """Whether a RANGE is a reduced axis (``(id, AxisType.REDUCE)``)."""
    return rng.op is Ops.RANGE and rng.arg[-1] is AxisType.REDUCE


def affine(idx) -> tuple | None:
    """``({range: coefficient}, offset)`` of an affine index, or None."""
    if idx.op is Ops.RANGE:
        return {idx: 1}, 0
    if idx.op is Ops.CONST and type(idx.arg) is int:
        return {}, idx.arg
    if idx.op is Ops.ADD and len(idx.src) == 2:
        ga, gb = affine(idx.src[0]), affine(idx.src[1])
        if ga is None or gb is None:
            return None
        (a, oa), (b, ob) = ga, gb
        out = dict(a)
        for r, c in b.items():
            out[r] = out.get(r, 0) + c
        return {r: c for r, c in out.items() if c}, oa + ob
    if idx.op is Ops.MUL and len(idx.src) == 2:
        for held, k in (idx.src, idx.src[::-1]):
            if k.op is Ops.CONST and type(k.arg) is int:
                got = affine(held)
                if got is None:
                    return None
                return {r: c * k.arg for r, c in got[0].items() if c * k.arg}, got[
                    1
                ] * k.arg
    return None


def gated(u):
    """``(gate, idx)`` for ``WHERE(gate, idx, Invalid)`` (a padded read), or
    None."""
    if u.op is Ops.WHERE and len(u.src) == 3:
        inv = u.src[2]
        if inv.op is Ops.CONST and str(inv.arg) == "Invalid":
            return u.src[0], u.src[1]
    return None


def param_of(u):
    """The PARAM an INDEX reads, through a view of it."""
    while u.op not in (Ops.PARAM, Ops.BUFFER) and u.src:
        u = u.src[0]
    return u if u.op is Ops.PARAM else None


def summarise(ast) -> str:
    """The ops an ast holds, for a refusal that says what it saw."""
    return ", ".join(sorted({u.op.name for u in ast.toposort()}))


__all__ = [
    "affine",
    "counted",
    "envelope",
    "extent",
    "gated",
    "param_of",
    "reduces",
    "summarise",
]
