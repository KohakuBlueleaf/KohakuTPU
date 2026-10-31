"""Indices made affine on the domain where they are read.

tinygrad builds a sliding window (`Tensor._pool`) by repeating its input and
reshaping the repeats; the backward of that view reads
``(r * i + y) % (i + d) // s`` under gates. No stride row describes it over
the whole loop nest, but on the points its gates let through it is affine:

- `collapse`: a sum whose support (`support`) lets through at most one point
  of some reduced ranges for each point of the others loses those ranges,
  each now an affine function of the others (a pool's backward);
- `fit`: a read's index is replaced by the integer affine index equal to it
  wherever its gate lets it through (a convolution's input gradient).

Either may first split a range into mixed-radix digits (``y = 2 y1 + y0``).
Every form is checked point for point in numpy (`fold.evaluate`); an
expression too large to evaluate is left as it is.
"""

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import uops as U

#: Points an index-only expression is evaluated over, at most.
MAX_POINTS = 1 << 21


def normalise(ast, digits: dict) -> tuple:
    """``(ast, digits)``: one-point reductions collapsed, every read's index
    fitted, nested sums merged; `digits` (envelope range -> its digits)
    extended by the splits made."""
    ast, digits = fit(*collapse(_unmasked(ast), digits))
    return merge(_unmasked(ast)), digits


def _unmasked(ast):
    """A sum's summand masked Invalid (a float `where` with an Invalid arm)
    adds nothing: that arm a zero."""
    subs = {}
    for red in ast.toposort():
        if not _sum(red):
            continue
        x = red.src[0]
        while x.op is Ops.CAST:
            x = x.src[0]
        if (
            x.op is Ops.WHERE
            and dtypes.is_float(x.dtype)
            and any(_invalid(a) for a in x.src[1:])
        ):
            arms = [a.const_like(0) if _invalid(a) else a for a in x.src[1:]]
            subs[x] = x.replace(src=(x.src[0], *arms))
    return ast.substitute(subs) if subs else ast


def _invalid(u) -> bool:
    return u.op is Ops.CONST and str(u.arg) == "Invalid"


def _sum(u) -> bool:
    op = u.arg[0] if isinstance(u.arg, tuple) else u.arg
    return u.op is Ops.REDUCE and op is Ops.ADD


def _peel(u):
    """`u` under its float-to-float casts (a precision step, not a value)."""
    while (
        u.op is Ops.CAST
        and dtypes.is_float(u.dtype)
        and dtypes.is_float(u.src[0].dtype)
    ):
        u = u.src[0]
    return u


def merge(ast):
    """A sum of a sum (under zero-else index `where`s and float casts, or
    not) as one sum over both range sets, the `where`s one gate on its
    summand, in the outer sum's precision: one contraction a convolution's
    gradient is."""
    while True:
        for u in ast.toposort():
            if not _sum(u):
                continue
            x, on = _peel(u.src[0]), None
            while x.op is Ops.WHERE and F.index_only(x.src[0]) and _is_zero(x.src[2]):
                on = x.src[0] if on is None else on & x.src[0]
                x = _peel(x.src[1])
            if not _sum(x):
                continue
            body, inner = x.src[0], x.src[1:]
            if on is not None:
                body = on.where(body, body.const_like(0))
            if body.dtype != u.src[0].dtype:
                body = body.cast(u.src[0].dtype)
            ast = ast.substitute({u: u.replace(src=(body, *u.src[1:], *inner))})
            break
        else:
            return ast


# ------------------------------------------------------------- helpers
def _ranges(u) -> list:
    return sorted(
        {x for x in u.toposort() if x.op is Ops.RANGE}, key=lambda r: r.arg[0]
    )


def _is_zero(u) -> bool:
    return u.op is Ops.CONST and (str(u.arg) == "Invalid" or u.arg == 0)


def _and(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def support(u):
    """An index-only condition outside which `u` is zero (None: none
    known): a `where` whose other branch is zero or Invalid, a gated read,
    either factor of a product."""
    if u.op is Ops.CAST:
        return support(u.src[0])
    if u.op is Ops.MUL:
        return _and(support(u.src[0]), support(u.src[1]))
    if u.op is Ops.WHERE and F.index_only(u.src[0]) and _is_zero(u.src[2]):
        return _and(u.src[0], support(u.src[1]))
    if u.op is Ops.INDEX and len(u.src) > 1:
        g = U.gated(u.src[1])
        return g[0] if g is not None else None
    return None


def _radices(u, r) -> list:
    """Divisors of `r`'s extent that `u` divides or reduces by: where a
    digit split of `r` may make `u` affine."""
    n = U.extent(r)
    out = set()
    for x in u.toposort():
        if x.op in (Ops.FLOORDIV, Ops.FLOORMOD) and x.src[1].op is Ops.CONST:
            c = x.src[1].arg
            if isinstance(c, int) and 1 < c < n and n % c == 0:
                out.add(c)
    return sorted(out)


def _split(ast, digits: dict, r, c: int) -> tuple:
    """`r` as two digits ``hi * c + lo`` everywhere in `ast` (a reduction
    over `r` reduces over both). ``(ast, digits, {r: (hi, lo)})``."""
    nxt = 1 + max(x.arg[0] for x in ast.toposort() if x.op is Ops.RANGE)
    hi = UOp.range(U.extent(r) // c, nxt, r.arg[1])
    lo = UOp.range(c, nxt + 1, r.arg[1])
    reds = {
        u: u.replace(
            src=(
                u.src[0],
                *(d for s in u.src[1:] for d in ((hi, lo) if s is r else (s,))),
            )
        )
        for u in ast.toposort()
        if u.op is Ops.REDUCE and r in u.src[1:]
    }
    if reds:
        ast = ast.substitute(reds)
    ast = ast.substitute({r: hi * c + lo})
    new = {
        k: tuple(x for d in ds for x in ((hi, lo) if d is r else (d,)))
        for k, ds in digits.items()
    }
    if not any(r in ds for ds in digits.values()):
        new[r] = (hi, lo)
    return ast, new, {r: (hi, lo)}


def _grid(us: list, ranges: list):
    """Each of `us` over `ranges` (full arrays, one axis each), or None when
    the nest is too large or an expression reads data."""
    shape = tuple(U.extent(r) for r in ranges)
    if int(np.prod(shape)) > MAX_POINTS:
        return None
    memo: dict = {}
    try:
        return [np.broadcast_to(F.evaluate(u, tuple(ranges), memo), shape) for u in us]
    except ValueError:
        return None


def _coords(ranges: list) -> np.ndarray:
    """Every point of `ranges` in row-major order, one column a range."""
    if not ranges:
        return np.zeros((1, 0), np.int64)
    axes = np.meshgrid(*(np.arange(U.extent(r)) for r in ranges), indexing="ij")
    return np.stack([a.reshape(-1) for a in axes], 1).astype(np.int64)


def _affine_fit(x: np.ndarray, y: np.ndarray) -> tuple | None:
    """Integer ``(coefficients, constant)`` with ``x @ c + c0 == y`` on every
    row, or None."""
    if not len(y):
        return np.zeros(x.shape[1], np.int64), 0
    a = np.concatenate([x, np.ones((len(x), 1), np.int64)], 1)
    # Columns equal on these rows (digits a gate ties) share one weight:
    # solve over independent ones, the constant first, the rest weight 0.
    keep: list = []
    for j in [a.shape[1] - 1, *range(a.shape[1] - 1)]:
        if np.linalg.matrix_rank(a[:, [*keep, j]].astype(np.float64)) > len(keep):
            keep.append(j)
    sol = np.linalg.lstsq(
        a[:, keep].astype(np.float64), y.astype(np.float64), rcond=None
    )[0]
    c = np.zeros(a.shape[1], np.int64)
    c[keep] = np.rint(sol).astype(np.int64)
    if not np.array_equal(a @ c, y.astype(np.int64)):
        return None
    return c[:-1], int(c[-1])


def _build(ranges: list, coef, c0: int, like):
    out = None
    for r, v in zip(ranges, coef):
        if v:
            t = r if v == 1 else r * int(v)
            out = t if out is None else out + t
    if out is None:
        return like.const_like(c0)
    return out + c0 if c0 else out


def _with_splits(ast, digits, exprs: list, ranges: list, solve):
    """`solve(exprs, ranges)` as they are, else after splitting one of
    `ranges` (then each at its first radix) where `exprs` divide by.
    ``(ast, digits, exprs, ranges, result, substitution made)``, or None."""
    got = solve(exprs, ranges)
    if got is not None:
        return ast, digits, exprs, ranges, got, {}
    cands = list(
        dict.fromkeys((r, c) for r in ranges for e in exprs for c in _radices(e, r))
    )
    tries = [[rc] for rc in cands]
    first: dict = {}
    for r, c in cands:
        first.setdefault(r, c)
    if len(first) > 1:
        tries.append(list(first.items()))
    for t in tries:
        a2, d2, rs2, ex2, made = ast, digits, list(ranges), list(exprs), {}
        for r, c in t:
            a2, d2, sub = _split(a2, d2, r, c)
            made[r] = sub[r][0] * c + sub[r][1]
            ex2 = [e.substitute({r: made[r]}) for e in ex2]
            at = rs2.index(r)
            rs2[at : at + 1] = list(sub[r])
        got = solve(ex2, rs2)
        if got is not None:
            return a2, d2, ex2, rs2, got, made
    return None


# ------------------------------------------------------------- collapse
def collapse(ast, digits: dict) -> tuple:
    """Every one-point reduction (see the module) collapsed."""
    while True:
        for red in ast.toposort():
            op = red.arg[0] if isinstance(red.arg, tuple) else red.arg
            if red.op is not Ops.REDUCE or op is not Ops.ADD:
                continue
            got = _collapse_one(ast, digits, red)
            if got is not None:
                ast, digits = got
                break
        else:
            return ast, digits


def _collapse_one(ast, digits, red):
    sup = support(red.src[0])
    if sup is None:
        return None
    reduced = list(red.src[1:])
    inside = _ranges(sup)
    rs = [r for r in reduced if r in inside]
    ro = [r for r in inside if r not in reduced]
    if not rs:
        return None

    def solve(exprs, outer):
        g = _grid(exprs, [*outer, *rs])
        if g is None:
            return None
        live = g[0].reshape(len(_coords(outer)), -1).astype(bool)
        count = live.sum(1)
        if (count > 1).any():
            return None
        pts = _coords(rs)[live.argmax(1)]
        xo = _coords(outer)
        fits = []
        for j, r in enumerate(rs):
            f = _affine_fit(xo[count == 1], pts[count == 1, j])
            if f is None:
                return None
            # Off the support too, the point stays inside its range.
            v = xo @ f[0] + f[1]
            if (v < 0).any() or (v >= U.extent(r)).any():
                return None
            fits.append(f)
        return fits

    found = _with_splits(ast, digits, [sup], ro, solve)
    if found is None:
        return None
    ast, digits, _, ro, fits, _ = found
    red = next(
        u
        for u in ast.toposort()
        if u.op is Ops.REDUCE and set(rs) <= set(u.src[1:]) and u.arg == red.arg
    )
    body = red.src[0].substitute(
        {r: _build(ro, c, c0, r) for r, (c, c0) in zip(rs, fits)}
    )
    # A summand masked Invalid adds nothing: out of the sum it is a zero.
    body = body.substitute(
        {
            u: u.src[0].where(u.src[1], u.src[1].const_like(0))
            for u in body.toposort()
            if u.op is Ops.WHERE
            and dtypes.is_float(u.dtype)
            and str(u.src[2].arg) == "Invalid"
        }
    )
    left = tuple(r for r in red.src[1:] if r not in rs)
    return (
        ast.substitute({red: red.replace(src=(body, *left)) if left else body}),
        digits,
    )


# ------------------------------------------------------------- fit
#: A node's value reaches the output everywhere.
_ALL = object()


def _either(a, b):
    if a is _ALL or b is _ALL:
        return _ALL
    return b if a is None or a is b else a | b


def _conjuncts(c) -> list:
    if c.op is Ops.AND:
        return [x for s in c.src for x in _conjuncts(s)]
    return [c]


def _both(c, s):
    """``c & s``, each conjunct once (`_ALL`: no condition)."""
    if s is None:
        return c
    if c is _ALL:
        return s
    terms = list(dict.fromkeys([*_conjuncts(c), *_conjuncts(s)]))
    out = terms[0]
    for t in terms[1:]:
        out = out & t
    return out


def used(ast) -> dict:
    """``{node: condition}``: the index-only condition outside which the
    node's value does not reach the output (`_ALL`: everywhere) -- an arm of
    an index-only `where` not where the other is taken."""
    cond: dict = {ast: _ALL}
    for p in reversed(ast.toposort()):
        c = cond.get(p)
        if c is None:
            continue
        if (
            p.op is Ops.WHERE
            and F.index_only(p.src[0])
            and p.src[0].dtype == dtypes.bool
        ):
            on = p.src[0]
            kids = [
                (on, c),
                (p.src[1], _both(c, on)),
                (p.src[2], _both(c, on.ne(True))),
            ]
        else:
            kids = [(s, c) for s in p.src]
        for s, k in kids:
            cond[s] = _either(cond.get(s), k)
    return cond


def fit(ast, digits: dict) -> tuple:
    """Every read whose index is not affine given the affine index equal to
    it wherever its gate lets it through and its value reaches the output
    (`used`); elsewhere the read is invalid (reads zero, where nothing uses
    it)."""
    while True:
        reach = used(ast)
        for x in ast.toposort():
            if x.op is not Ops.INDEX or len(x.src) < 2 or U.param_of(x) is None:
                continue
            g = U.gated(x.src[1])
            gate, inner = g if g is not None else (None, x.src[1])
            if U.affine(inner) is not None or not F.index_only(inner):
                continue
            ctx = reach.get(x, _ALL)
            got = _fit_one(ast, digits, x, gate, inner, None if ctx is _ALL else ctx)
            if got is not None:
                ast, digits = got
                break
        else:
            return ast, digits


def _terms(u) -> list:
    return [t for s in u.src for t in _terms(s)] if u.op is Ops.ADD else [u]


def _groups(terms: list) -> list:
    """`terms` grouped by the ranges they share (transitively)."""
    groups: list = []  # [(ranges, terms)]
    for t in terms:
        rs = set(_ranges(t))
        hit = [g for g in groups if g[0] & rs]
        groups = [g for g in groups if not g[0] & rs]
        merged = [x for g in hit for x in g[1]] + [t]
        groups.append((rs.union(*(g[0] for g in hit)), merged))
    return groups


def _solve(ex, rs):
    """``((coefficients, constant), own ranges)`` of the affine form equal
    to `ex[0]` wherever `ex[1]` (if given) holds for some value of the other
    ranges, or None."""
    own = [r for r in rs if r in _ranges(ex[0])]
    g = _grid([ex[0]], own)
    if g is None:
        return None
    n = len(_coords(own))
    val = g[0].reshape(-1)
    if len(ex) > 1:
        ok = _held(ex[1], own, [r for r in rs if r not in own])
        if ok is None:
            return None
    else:
        ok = np.ones(n, bool)
    got = _affine_fit(_coords(own)[ok], val[ok])
    return None if got is None else (got, own)


def _held(cond, own: list, others: list):
    """`cond` over `own` (row-major, flat): where it holds for some value of
    `others`. Past `MAX_POINTS` a block of the largest other range at a time
    (that range renamed onto a shorter one, shifted)."""
    n = int(np.prod([U.extent(r) for r in own])) if own else 1
    size = n * (int(np.prod([U.extent(r) for r in others])) if others else 1)
    if size <= MAX_POINTS:
        g = _grid([cond], [*own, *others])
        return None if g is None else g[0].reshape(n, -1).any(1).astype(bool)
    if not others:
        return None
    big = max(others, key=U.extent)
    rest = [r for r in others if r is not big]
    e = U.extent(big)
    step = max(1, MAX_POINTS // (size // e))
    nxt = 1 + max(x.arg[0] for x in cond.toposort() if x.op is Ops.RANGE)
    out = np.zeros(n, bool)
    for s in range(0, e, step):
        k = min(step, e - s)
        sub = UOp.range(k, nxt, big.arg[1])
        got = _held(cond.substitute({big: sub + s}), own, [*rest, sub])
        if got is None:
            return None
        out |= got
    return out


def _fit_one(ast, digits, x, gate, inner, ctx):
    """`x`'s index fitted where `gate` and `ctx` (where its value is used,
    None: everywhere) hold; with `ctx`, `x` alone is rewritten, else every
    read through its gated index. The non-affine terms are fitted a group
    sharing ranges at a time, over the conditions that read those ranges --
    a superset of the points used, so a fit there holds on them (separate
    axes never multiply their grids)."""
    idx = x.src[1]
    terms = _terms(inner)
    on = gate if ctx is None else _both(ctx, gate)
    made: dict = {}
    new = None
    rest = [t for t in terms if U.affine(t) is None]
    joint = _ranges(UOp.sink(*rest, *([] if on is None else [on])))
    points = int(np.prod([U.extent(r) for r in joint])) if joint else 1
    # One fit over every range while its grid is evaluable, else a group at
    # a time (separate axes never multiply their grids).
    groups = [(set(), rest)] if points <= MAX_POINTS else _groups(rest)
    for _, group in groups:
        part = group[0]
        for t in group[1:]:
            part = part + t
        part = part.substitute(made) if made else part
        mine = set(_ranges(part))
        conds = [] if on is None else _conjuncts(on.substitute(made) if made else on)
        cond = None
        for c in conds:
            if len(groups) == 1 or mine & set(_ranges(c)):
                cond = c if cond is None else cond & c
        exprs = [part] if cond is None else [part, cond]
        found = _with_splits(ast, digits, exprs, _ranges(UOp.sink(*exprs)), _solve)
        if found is None:
            return None
        ast, digits, exprs, _, ((coef, c0), own), more = found
        made = {**made, **more}
        term = _build(own, coef, c0, exprs[0])
        new = term if new is None else new + term
    assert new is not None
    # A later group may split a range an earlier one's form reads.
    new = new.substitute(made)
    for t in terms:
        if U.affine(t) is not None:
            new = new + t.substitute(made)
    if on is not None and made:
        on = on.substitute(made)
    to = new if on is None else new.valid(on)
    if ctx is not None:
        now_x = x.substitute(made) if made else x
        subs = {now_x: now_x.replace(src=(now_x.src[0], to, *now_x.src[2:]))}
        return ast.substitute(subs), digits
    # Every read through this same gated index, as it is now.
    now = idx.substitute(made)
    subs = {
        u: u.replace(src=(u.src[0], to, *u.src[2:]))
        for u in ast.toposort()
        if u.op is Ops.INDEX and len(u.src) > 1 and u.src[1] is now
    }
    if not subs:
        return None
    return ast.substitute(subs), digits


__all__ = ["MAX_POINTS", "collapse", "fit", "merge", "normalise", "support"]
