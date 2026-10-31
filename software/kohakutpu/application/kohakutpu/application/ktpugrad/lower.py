"""One tinygrad kernel ast as a plan for this machine, or a refusal by name
(never computed some other way): ``const`` (reads no buffer: folded while
compiling and uploaded), ``copy`` (one read stored at its own index),
``matmul`` (a sum over one range of a product of A [M, K] and B [N, K],
batched or not, under any elementwise epilogue) or ``vector`` (an elementwise
expression or a row reduction over operands read as whole rows, one row, a
value a row, or one value). The expression is `kernels.expr`: tinygrad's ALU
ops as this machine's (``1/sqrt`` one `rsqrt`, a bool cast a compare), an
index-only subexpression inside data math a folded table operand.
"""

import itertools
from dataclasses import dataclass, field

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import domain as D
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.language.numerics import fp16


class Refused(RuntimeError):
    """A kernel this machine has no lowering for, and why."""

    clause = "TG.PLAN.010"


@dataclass(frozen=True)
class Access:
    """One read: param `slot` (None: a folded table), its dtype, its stride
    row over the kernel's ranges, its element offset."""

    slot: int | tuple | None  # a param slot, or a runner temporary's key
    dtype: object
    coef: tuple  # ((range, coefficient), ...)
    off: int
    table: object = None  # the folded array, for slot None
    #: a padded read's bound (a table): the runner splits the loop nest into
    #: boxes it is constant over, so no read leaves its buffer
    gate: bool = False

    def c(self, r) -> int:
        return dict(self.coef).get(r, 0)


@dataclass
class Plan:
    kind: str  # "const" | "copy" | "vector" | "matmul"
    ast: object
    name: str
    out_slot: int
    out_dtype: object
    out_shape: tuple
    out_ranges: tuple  # outermost first
    expr: object = None
    accesses: list = field(default_factory=list)
    reduce_ranges: tuple = ()
    #: each reduction's ranges, by `id` of its `X.Reduce` (several sets:
    #: the runner materialises the inner ones first)
    reductions: dict = field(default_factory=dict)
    value: np.ndarray | None = None  # const: the output
    #: matmul: A and B reads, the m / n / k ranges, batch ranges
    a: Access | None = None
    b: Access | None = None
    mnk: tuple = ()
    batch: tuple = ()
    #: the output's element strides by `out_ranges` when it is a window of
    #: its buffer (a box of a padded loop nest); None: dense
    out_strides: tuple | None = None

    @property
    def out_n(self) -> int:
        return int(np.prod(self.out_shape)) if self.out_shape else 1


#: tinygrad ALU op -> expression op (same operand order).
ALU = {
    Ops.ADD: "add",
    Ops.SUB: "sub",
    Ops.MUL: "mul",
    Ops.FDIV: "div",
    Ops.MAX: "max",
    Ops.NEG: "neg",
    Ops.RECIPROCAL: "recip",
    Ops.EXP2: "exp2",
    Ops.LOG2: "log2",
    Ops.SQRT: "sqrt",
    Ops.SIN: "sin",
    Ops.TRUNC: "trunc",
    Ops.POW: "pow",
    Ops.CMPLT: "cmplt",
    Ops.CMPNE: "cmpne",
    Ops.CMPEQ: "cmpeq",
    Ops.WHERE: "where",
    Ops.MULACC: "fma",
}
#: ALU ops on bools only (0.0 / 1.0 in a lane).
BOOL = {Ops.AND: "and", Ops.OR: "or", Ops.XOR: "xor"}
REDUCE = {Ops.ADD: "sum", Ops.MAX: "max", Ops.MUL: "prod"}


def plan(ast) -> Plan:
    """The plan for `ast`; raises `Refused` naming the construct."""
    got = U.envelope(ast)
    if got is None:
        raise Refused(f"not one stored loop nest; it holds {U.summarise(ast)}")
    ast, digits = D.normalise(*_unfold(ast))
    store = ast.src[0].src[0] if ast.src[0].op is Ops.END else ast.src[0]
    ranges = tuple(d for r in got[1] for d in digits.get(r, (r,)))
    idx, value = store.src[0], store.src[1]
    out_param = U.param_of(idx)
    if idx.op is not Ops.INDEX or out_param is None:
        raise Refused("the store is not into a buffer")
    oa = U.affine(idx.src[1]) if len(idx.src) > 1 else ({}, 0)
    if oa is None or oa[1] != 0:
        raise Refused("the store's index is not one dense walk of the loop nest")
    order = _order(oa[0], ranges)
    name = getattr(ast.arg, "name", "kernel")
    shape = tuple(U.extent(r) for r in order)
    base = Plan(
        "", ast, name, out_param.arg.slot, _scalar(out_param.dtype), shape, order
    )
    if F.index_only(value):
        base.kind = "const"
        base.value = _broadcast(F.evaluate(value, order), shape).astype(
            F.np_dtype(base.out_dtype)
        )
        return base
    reader = _Reader(order)
    e = reader.read(value)
    base.accesses = reader.accesses
    base.expr = e
    base.reduce_ranges = reader.reduce_ranges
    base.reductions = reader.reductions
    mm = (
        _matmul(base, value, reader)
        if len(set(reader.reductions.values())) <= 1
        else None
    )
    if mm is not None:
        return mm
    if isinstance(e, X.Load) and not reader.reduce_ranges:
        acc = reader.accesses[e.i]
        if (
            acc.slot is not None
            and dict(acc.coef) == oa[0]
            and acc.off == 0
            and acc.dtype == base.out_dtype
        ):
            base.kind = "copy"
            return base
    base.kind = "vector"
    return base


def _quotient(u) -> tuple | None:
    """``(range, a)`` when `u` is ``r // a`` (nested floor divisions by
    constants multiply; a bare range is ``r // 1``), else None."""
    if u.op is Ops.RANGE:
        return (u, 1) if U.counted(u) is not None else None
    if u.op is Ops.FLOORDIV and u.src[1].op is Ops.CONST:
        inner = _quotient(u.src[0])
        if inner is not None and isinstance(u.src[1].arg, int):
            return inner[0], inner[1] * u.src[1].arg
    return None


def _unfold(ast) -> tuple:
    """A reshape that divides, made affine: a range `r` read as ``r // d`` /
    ``r % d``, nested (``(r // a) % b`` splits at a and a * b; each split
    dividing the next and the extent), becomes its mixed-radix digits,
    ``r = sum(digit * place)``, every division over them folded (`_affine`).
    ``(ast, {r: digits outermost first})``."""
    divs: dict = {}
    for u in ast.toposort():
        if u.op not in (Ops.FLOORDIV, Ops.FLOORMOD) or u.src[1].op is not Ops.CONST:
            continue
        base = _quotient(u.src[0])
        if base is None or not isinstance(u.src[1].arg, int):
            continue
        # (r // a) // b and (r // a) % b split r at a and at a * b.
        r, a = base
        divs.setdefault(r, set()).update(d for d in (a, a * u.src[1].arg) if d > 1)
    nxt = 1 + max((r.arg[0] for r in ast.toposort() if r.op is Ops.RANGE), default=0)
    split, digits = {}, {}
    for r, ds in divs.items():
        n = U.extent(r)
        chain = sorted(d for d in ds if isinstance(d, int) and 1 < d < n)
        if (
            not chain
            or any(n % d for d in chain)
            or any(b % a for a, b in itertools.pairwise(chain))
        ):
            continue
        terms = []
        for lo, hi in itertools.pairwise([1, *chain, n]):
            terms.insert(0, (UOp.range(hi // lo, nxt, r.arg[1]), lo))
            nxt += 1
        split[r], digits[r] = tuple(terms), tuple(d for d, _ in terms)
    if not split:
        return ast, {}

    # A reduction over a split range reduces over its digits.
    reduces = {
        u: u.replace(
            src=(u.src[0], *(d for s in u.src[1:] for d in digits.get(s, (s,))))
        )
        for u in ast.toposort()
        if u.op is Ops.REDUCE and any(s in digits for s in u.src[1:])
    }
    if reduces:
        ast = ast.substitute(reduces)

    # Each outermost division over a split range folded, then the bare uses.
    folds = {}
    for u in ast.toposort():
        if u.op in (Ops.FLOORDIV, Ops.FLOORMOD) and any(r in split for r in u.ranges):
            form = _affine(u, split)
            if form is not None:
                folds[u] = _build(form, u)
    inner = {v for u in folds for v in u.toposort() if v is not u}
    ast = ast.substitute({u: e for u, e in folds.items() if u not in inner})
    return (
        ast.substitute({r: _build((dict(t), 0), r) for r, t in split.items()}),
        digits,
    )


def _affine(u, split: dict) -> tuple | None:
    """`u` as ``({range: coefficient}, constant)``, a split range as its
    digits (`split`: range -> ((digit, place), ...)). ``x // c`` and
    ``x % c`` fold when the terms whose coefficient c does not divide stay in
    [0, c): those are the remainder, the rest divided by c the quotient."""
    if u.op is Ops.CONST and isinstance(u.arg, int):
        return {}, u.arg
    if u.op is Ops.RANGE:
        return (dict(split[u]), 0) if u in split else ({u: 1}, 0)
    if u.op not in (Ops.ADD, Ops.MUL, Ops.FLOORDIV, Ops.FLOORMOD):
        return None
    if u.op in (Ops.ADD, Ops.MUL):
        a, b = (_affine(s, split) for s in u.src)
        if a is None or b is None:
            return None
        if u.op is Ops.ADD:
            terms = dict(a[0])
            for r, v in b[0].items():
                terms[r] = terms.get(r, 0) + v
            return terms, a[1] + b[1]
        if a[0] and b[0]:
            return None
        (terms, c0), k = (a, b[1]) if a[0] else (b, a[1])
        return {r: v * k for r, v in terms.items()}, c0 * k

    c = u.src[1].arg if u.src[1].op is Ops.CONST else None
    x = _affine(u.src[0], split)
    if x is None or not isinstance(c, int) or c <= 0:
        return None
    q, rem = divmod(x[1], c)
    high = {r: v // c for r, v in x[0].items() if v % c == 0}
    low = {r: v for r, v in x[0].items() if v % c}
    lo = rem + sum(min(0, v * (U.extent(r) - 1)) for r, v in low.items())
    hi = rem + sum(max(0, v * (U.extent(r) - 1)) for r, v in low.items())
    if lo < 0 or hi >= c:
        return None
    return (high, q) if u.op is Ops.FLOORDIV else (low, rem)


def _build(form: tuple, like):
    """``sum(range * coefficient) + constant`` as an index expression."""
    terms, c0 = form
    out = None
    for r, v in terms.items():
        if v:
            t = r * v if v != 1 else r
            out = t if out is None else out + t
    if out is None:
        return like.const_like(c0)
    return out + c0 if c0 else out


def _scalar(dt):
    """A buffer's element type (a pointer type's base, where it is one)."""
    return getattr(dt, "base", dt)


def _order(coef: dict, ranges: tuple) -> tuple:
    """The store's ranges, outermost first, checked to be one dense
    row-major walk."""
    if set(coef) - set(ranges):
        raise Refused("the store reads a range outside its loop nest")
    if any(r not in coef and U.extent(r) > 1 for r in ranges):
        raise Refused("a loop range the store does not index (a broadcast store)")
    order = tuple(sorted(coef, key=lambda r: -coef[r]))
    want = 1
    for r in reversed(order):
        if coef[r] != want:
            raise Refused("the store is not one dense row-major walk of its loop nest")
        want *= U.extent(r)
    return order


def _broadcast(a, shape):
    return np.broadcast_to(np.asarray(a), shape).copy()


class _Reader:
    """An ast value as an expression; each read becomes an `Access`."""

    def __init__(self, out_ranges: tuple) -> None:
        self.out_ranges = out_ranges
        self.accesses: list = []
        self.reduce_ranges: tuple = ()
        self.reductions: dict = {}
        self.memo: dict = {}
        self.imemo: dict = {}

    def read(self, u):
        if u in self.memo:
            return self.memo[u]
        out = self._read(u)
        self.memo[u] = out
        return out

    def _read(self, u):
        if F.index_only(u, self.imemo):
            if u.op is Ops.CONST:
                v = u.arg
                if isinstance(v, bool):
                    return X.Const(1.0 if v else 0.0)
                if isinstance(v, (int, float)):
                    return X.Const(float(v))
                raise Refused(f"a constant {v!r}")
            return self._table(u)
        if u.op is Ops.INDEX:
            return self._load(u)
        if u.op is Ops.CAST:
            return self._cast(u)
        if u.op is Ops.BITCAST:
            raise Refused(
                "a BITCAST reinterprets bits; lanes hold E8M15 values, not bits"
            )
        if u.op is Ops.REDUCE:
            return self._reduce(u)
        if u.op is Ops.RECIPROCAL and u.src[0].op is Ops.SQRT:
            return X.op("rsqrt", self.read(u.src[0].src[0]))
        if u.op in ALU:
            args = [self.read(s) for s in u.src]
            if u.op is Ops.WHERE and isinstance(args[0], X.Const):
                return args[1] if args[0].value else args[2]
            return X.op(ALU[u.op], *args)
        if u.op in BOOL and u.dtype == dtypes.bool:
            return X.op(BOOL[u.op], *[self.read(s) for s in u.src])
        if u.op is Ops.THREEFRY:
            raise Refused(
                "THREEFRY on data: a bit-mixing hash has no lane operation "
                "(make random tensors with numpy, or the mover's `gen`)"
            )
        raise Refused(f"no lowering for {u.op.name}")

    def _cast(self, u):
        """A cast: to a bool, a compare with 0; to a float, the value."""
        to = u.dtype
        x = self.read(u.src[0])
        if to == dtypes.bool:
            return X.op("cmpne", x, 0.0)
        if not dtypes.is_float(to):
            raise Refused(f"a cast to {to}")
        return x

    def _reduce(self, u):
        op = u.arg[0] if isinstance(u.arg, tuple) else u.arg
        if op not in REDUCE:
            raise Refused(f"a {op} reduction")
        rs = tuple(u.src[1:])
        if any(r.op is not Ops.RANGE for r in rs):
            raise Refused("a reduction over something other than loop ranges")
        # Every reduced range, for "is there a reduction"; each set by node.
        self.reduce_ranges = tuple(dict.fromkeys(self.reduce_ranges + rs))
        out = X.Reduce(REDUCE[op], self.read(u.src[0]))
        self.reductions[id(out)] = rs
        return out

    def _load(self, u):
        param = U.param_of(u)
        if param is None:
            raise Refused("a read of something other than a buffer")
        idx = u.src[1] if len(u.src) > 1 else None
        gate = None
        if idx is None:
            coef, off = {}, 0
        else:
            g = U.gated(idx)
            if g is not None:
                gate, idx = g
                if not F.index_only(gate, self.imemo):
                    raise Refused("a padded read whose bound reads data")
            got = U.affine(idx)
            if got is None:
                raise Refused(
                    "a read whose index is not affine in the loop (a reshape that divides)"
                )
            coef, off = got
        acc = Access(param.arg.slot, _scalar(param.dtype), tuple(coef.items()), off)
        if acc not in self.accesses:
            self.accesses.append(acc)
        load = X.Load(self.accesses.index(acc))
        if gate is None:
            return load
        # Out of bounds the read is 0 (tinygrad's padding).
        bound = self._table(gate, gate=True)
        if isinstance(bound, X.Const):
            return load if bound.value else X.Const(0.0)
        return X.op("where", bound, load, 0.0)

    def _table(self, u, gate: bool = False):
        """An index-only subexpression inside data math: folded over the
        ranges it reads, a compact row-major f16 table."""
        rs = [r for r in u.toposort() if r.op is Ops.RANGE]
        rs = sorted(set(rs), key=lambda r: (self._rank(r), r.arg[0]))
        arr = np.asarray(F.evaluate(u, tuple(rs)), np.float64)
        arr = np.broadcast_to(arr, tuple(U.extent(r) for r in rs))
        if arr.size and np.all(arr == arr.flat[0]):
            # One value over the whole nest: a constant.
            return X.Const(float(arr.flat[0]))
        coef, step = {}, 1
        for r in reversed(rs):
            coef[r] = step
            step *= U.extent(r)
        table = np.ascontiguousarray(fp16.write(arr))
        self.accesses.append(
            Access(None, dtypes.half, tuple(coef.items()), 0, table, gate)
        )
        return X.Load(len(self.accesses) - 1)

    def _rank(self, r) -> int:
        return (
            self.out_ranges.index(r) if r in self.out_ranges else len(self.out_ranges)
        )


def _peel(u):
    while (
        u.op is Ops.CAST
        and dtypes.is_float(u.dtype)
        and dtypes.is_float(u.src[0].dtype)
    ):
        u = u.src[0]
    return u


def _matmul(base: Plan, value, reader: _Reader) -> Plan | None:
    """`base` as a contraction when its one reduction sums a product of two
    reads, A [.., M, K] and B [.., N, K] in any layout, K one range or
    several (a head merge folded in); else None. ``mnk`` is ``(m, n, ks)``."""
    reds = [u for u in value.toposort() if u.op is Ops.REDUCE]
    if len(reds) != 1:
        return None
    red = reds[0]
    op = red.arg[0] if isinstance(red.arg, tuple) else red.arg
    if op is not Ops.ADD or len(red.src) < 2:
        return None
    mul = _peel(red.src[0])
    if mul.op is not Ops.MUL:
        return None
    loads = [_peel(s) for s in mul.src]
    if any(x.op is not Ops.INDEX for x in loads):
        return None
    ks = tuple(r for r in red.src[1:] if U.extent(r) > 1) or (red.src[1],)
    reads = [reader.read(x) for x in loads]
    if not all(isinstance(r, X.Load) for r in reads):
        return None
    accs = [reader.accesses[r.i] for r in reads if isinstance(r, X.Load)]
    for a, b in (accs, accs[::-1]):
        got = _mnk(base, a, b, ks)
        if got is not None:
            break
    else:
        return None
    m, n, batch = got
    k = ks
    # The epilogue: the reduction's value becomes Load(0), every other read
    # shifts up by one.
    shifted = _shift(base.expr, reader, X.Load(0))
    return Plan(
        "matmul",
        base.ast,
        base.name,
        base.out_slot,
        base.out_dtype,
        base.out_shape,
        base.out_ranges,
        expr=shifted[0],
        accesses=shifted[1],
        reduce_ranges=k,
        a=a,
        b=b,
        mnk=(m, n, k),
        batch=batch,
    )


def _mnk(base: Plan, a: Access, b: Access, ks: tuple):
    """``(m range, n range, batch ranges)`` when `a` reads [.., M, K] and `b`
    [.., N, K] in any layout (the runner lays operands out first), N the
    output's innermost range; A's other ranges (B reading none of them) are
    batches over a shared B."""
    if any(not a.c(k) or not b.c(k) for k in ks):
        return None
    live = [r for r in base.out_ranges if U.extent(r) > 1]
    ms = [r for r in live if a.c(r) and not b.c(r)]
    ns = [r for r in live if b.c(r) and not a.c(r)]
    if not ms or len(ns) != 1 or live[-1] is not ns[0]:
        return None
    m = ms[-1]
    batch = tuple(r for r in base.out_ranges if r is not m and r is not ns[0])
    return m, ns[0], batch


def _shift(e, reader: _Reader, prod):
    """The store's expression with the reduction replaced by `prod` (Load
    0) and the remaining reads renumbered from 1."""
    keep = []
    mapping = {}

    def walk(x):
        if isinstance(x, X.Reduce):
            return prod
        if isinstance(x, X.Load):
            if x.i not in mapping:
                keep.append(reader.accesses[x.i])
                mapping[x.i] = len(keep)
            return X.Load(mapping[x.i])
        if isinstance(x, X.Op):
            return X.Op(x.name, tuple(walk(a) for a in x.args))
        return x

    return walk(e), keep


__all__ = ["Access", "Plan", "Refused", "plan"]
