"""L2 -> L2 retiling: a node body to a node body, verified again after.

`retile(body, rows)`: every vector-core loop whose tiles are S rows of
contiguous memory (each view's axis-0 start steps by S a trip) runs S / rows
times as many trips of `rows`-row tiles; every value of S rows becomes one of
`rows`.
"""

from dataclasses import replace

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.lower.l2.emit import LowerError, affine
from kohakutpu.language.text.syntax import BinOp, Int, Name, Neg, Offset


def fold(t):
    """Constant folding of an index expression."""
    match t:
        case Offset(a, b):
            a, b = fold(a), fold(b)
            if isinstance(a, Int) and isinstance(b, Int):
                return Int(a.value + b.value)
            if a == Int(0):
                return b
            if b == Int(0):
                return a
            return Offset(a, b)
        case BinOp(op, a, b):
            a, b = fold(a), fold(b)
            if isinstance(a, Int) and isinstance(b, Int):
                v = {"-": a.value - b.value, "*": a.value * b.value}.get(op)
                if op == "/" and b.value:
                    v = a.value // b.value
                if op == "%" and b.value:
                    v = a.value % b.value
                if v is not None:
                    return Int(v)
            if op == "*" and Int(0) in (a, b):
                return Int(0)
            if op == "*" and a == Int(1):
                return b
            if op in ("*", "/") and b == Int(1):
                return a
            if op == "-" and b == Int(0):
                return a
            return BinOp(op, a, b)
        case Neg(x):
            x = fold(x)
            return Int(-x.value) if isinstance(x, Int) else Neg(x)
    return t


def subst(t, var: str, by):
    match t:
        case Name(text) if text == var:
            return by
        case Offset(a, b):
            return Offset(subst(a, var, by), subst(b, var, by))
        case BinOp(op, a, b):
            return BinOp(op, subst(a, var, by), subst(b, var, by))
        case Neg(x):
            return Neg(subst(x, var, by))
    return t


class _Retile:
    def __init__(self, par: L.Par, loop: L.For, rows: int) -> None:
        self.par, self.loop, self.rows = par, loop, rows
        sizes = {
            s.type.shape[0]
            for s in loop.body
            if isinstance(s, L.Load) and len(s.type.shape) == 2
        }
        self.S = sizes.pop() if len(sizes) == 1 else None

    def applies(self) -> bool:
        return self.S is not None and self.S > self.rows and self.S % self.rows == 0

    def view(self, v: L.View) -> L.View:
        a0 = v.axes[0]
        if a0.size != self.S:
            raise LowerError(f"retile: {v.name}'s axis 0 is not a {self.S}-row tile")
        _, d = affine(a0.lo, {self.par.var: self.par.lo}, self.loop.var)
        if d != self.S:
            raise LowerError(f"retile: {v.name} steps {d} rows a trip, not {self.S}")
        lo = Offset(
            subst(a0.lo, self.loop.var, Int(0)),
            BinOp("*", Name(self.loop.var), Int(self.rows)),
        )
        return replace(v, axes=(L.Axis(fold(lo), self.rows), *v.axes[1:]))

    def type(self, t: L.Type) -> L.Type:
        if t is not None and t.shape and t.shape[0] == self.S:
            return replace(t, shape=(self.rows, *t.shape[1:]))
        return t

    def stmt(self, s):
        match s:
            case L.Load():
                return replace(s, src=self.view(s.src), type=self.type(s.type))
            case L.Op():
                return replace(s, type=self.type(s.type))
            case L.Store(dst, value):
                if isinstance(value, L.Op):
                    value = replace(value, type=self.type(value.type))
                return L.Store(self.view(dst), value)
        raise LowerError(f"retile: no rule for {s!r}")

    def loop_out(self) -> L.For:
        f = self.S // self.rows
        body = tuple(self.stmt(s) for s in self.loop.body)
        return replace(self.loop, lo=self.loop.lo * f, hi=self.loop.hi * f, body=body)


def second_chain(loop: L.For) -> bool:
    """A reduction after the first op reading a row statistic: its tree can
    only overlap another tile's work, so the loop wants two tiles in
    registers (8-row tiles, the pipelined row form)."""
    ops = [s for s in loop.body if isinstance(s, L.Op)]
    stats = {o.name for o in ops if o.type and len(o.type.shape) == 1}
    seen = False
    for o in ops:
        reads = {a.name for a in o.args if isinstance(a, L.View)}
        if not seen and o.type and len(o.type.shape) == 2 and reads & stats:
            seen = True
        elif seen and o.op.startswith("reduce."):
            return True
    return False


def retile(body: L.Body, rows) -> L.Body:
    """`rows` an int, or `auto`: 8 where `second_chain`, else unchanged."""
    stmts = []
    for s in body.stmts:
        if isinstance(s, L.Par) and s.unit == "vc":
            inner = []
            for b in s.body:
                if isinstance(b, L.For):
                    want = (
                        (8 if second_chain(b) else None)
                        if rows == "auto"
                        else int(rows)
                    )
                    r = _Retile(s, b, want) if want else None
                    b = r.loop_out() if r and r.applies() else b
                inner.append(b)
            s = replace(s, body=tuple(inner))
        stmts.append(s)
    return replace(body, stmts=tuple(stmts), at={})


__all__ = ["fold", "retile", "subst"]
