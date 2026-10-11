"""The L2 verifier: names in scope, every view in bounds for every value of
its loop variables, shapes, spaces, and placement -- vector math on values of
its own instance, cluster sweeps on memory, memory the only way between units.
A name a `for` body rebinds from outside the loop is loop-carried: it keeps
its type, and the next iteration (and the code after the loop) sees the new
value.
"""

import itertools
from dataclasses import dataclass

import numpy as np
from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.text.syntax import BinOp, Int, Name, Neg, Offset, TextError

UNITS = {"vc": "VC", "mg": "MG"}
UNARY = {"neg", "abs", "exp2", "log2", "inv", "rsqrt", "copy"}
BINARY = {"add", "sub", "mul", "max", "min"}
TERNARY = {"fma", "sel"}
REDUCE = {"reduce.max", "reduce.sum", "reduce.min"}
MX7 = {"mx7", "mx7a", "mx7b"}
#: Enumerating loop values past this many points checks the corners only.
POINTS = 4096


@dataclass(frozen=True)
class Sym:
    type: L.Type
    #: The instance a register / L1 / accumulator value belongs to; None for memory.
    owner: object = None


def check(body: L.Body, units: dict) -> None:
    """Raise a `TextError` at the first statement that breaks a rule. `units`
    maps a unit kind (`VC`, `MG`) to how many the machine has."""
    _Check(body, units).run()


#: `BinOp` operators on ints; `/` is integer division.
ARITH = {
    "-": lambda x, y: x - y,
    "*": lambda x, y: x * y,
    "/": lambda x, y: x // y,
    "%": lambda x, y: x % y,
}


def value(t, env: dict) -> int:
    """An index expression's value under `env` (loop variable -> int)."""
    match t:
        case Int(v, _):
            return v
        case Name(text):
            return env[text]
        case Neg(x):
            return -value(x, env)
        case Offset(a, b):
            return value(a, env) + value(b, env)
        case BinOp(op, a, b) if op in ARITH:
            return ARITH[op](value(a, env), value(b, env))
    raise ValueError(f"not an index expression: {t!r}")


def _names(t) -> set:
    match t:
        case Name(text):
            return {text}
        case Neg(x):
            return _names(x)
        case Offset(a, b) | BinOp(_, a, b):
            return _names(a) | _names(b)
    return set()


class _Check:
    def __init__(self, body: L.Body, units: dict) -> None:
        self.body = body
        self.units = units

    def fail(self, node, message):
        st = self.body.at.get(id(node)) if node is not None else None
        line, col = (st.line, st.col) if st is not None else (0, 0)
        raise TextError(message, line, col, self.body.source, self.body.file)

    def run(self) -> None:
        scope = {n: Sym(t) for n, t in self.body.params}
        for n, t in self.body.params:
            if t.space != "dram":
                self.fail(
                    None, f"{self.body.name}.l2: parameter {n} is @dram, got @{t.space}"
                )
        self.block(self.body.stmts, scope, {}, None)

    # ------------------------------------------------------------ blocks
    def block(self, stmts, scope, loops, unit) -> dict:
        """Check `stmts` in a copy of `scope`; the copy, as the block leaves it."""
        scope = dict(scope)
        for s in stmts:
            self.stmt(s, scope, loops, unit)
        return scope

    def stmt(self, s, scope, loops, unit) -> None:
        match s:
            case L.Buffer(name, t):
                if unit is not None:
                    self.fail(s, "a buffer is the program's: at the top of the body")
                self.define(s, scope, name, Sym(t))
            case L.Par(var, lo, hi, kind, at, inner):
                if kind not in UNITS:
                    self.fail(s, f"par places on {', '.join(UNITS)}, got {kind}")
                inner_loops = {**loops, var: (lo, hi)}
                for env in self.points(inner_loops):
                    i = value(at, env)
                    if not 0 <= i < self.units[UNITS[kind]]:
                        self.fail(
                            s,
                            f"{kind}[{i}]: this machine has {self.units[UNITS[kind]]}",
                        )
                self.block(inner, scope, inner_loops, (kind, id(s)))
            case L.For(var, lo, hi, _, inner):
                if unit is None:
                    self.fail(s, "a for runs on a unit: inside a par")
                after = self.block(inner, scope, {**loops, var: (lo, hi)}, unit)
                for name, sym in scope.items():
                    if after[name] is not sym and after[name].type != sym.type:
                        self.fail(s, f"loop-carried {name} changes type in the loop")
                    scope[name] = after[name]
            case L.Load(name, src, t):
                self.on(s, unit, "vc", "a load")
                sym = self.memory(s, scope, src)
                shape = self.view(s, scope, src, loops)
                if t.space != "l1" or t.shape != shape or t.dtype != sym.type.dtype:
                    self.fail(s, f"this load is {sym.type.dtype}{list(shape)} @l1")
                self.define(s, scope, name, Sym(t, unit))
            case L.Store(dst, v):
                if unit is None:
                    self.fail(s, "a store runs on a unit: inside a par")
                sym = self.memory(s, scope, dst)
                shape = self.view(s, scope, dst, loops)
                if isinstance(v, L.Op):
                    got, dtype = self.op(s, v, scope, loops, unit)
                else:
                    got, dtype = self.register(s, scope, v, loops, unit)
                if got != shape and got != ():
                    self.fail(s, f"the value is {list(got)}, the view {list(shape)}")
                if (dtype in MX7) != (sym.type.dtype in MX7):
                    self.fail(s, f"a {dtype} value into a {sym.type.dtype} buffer")
            case L.Op(name, _, _, _, t):
                got, _ = self.op(s, s, scope, loops, unit)
                if t.shape != got:
                    self.fail(s, f"{s.op} gives {list(got)}, annotated {list(t.shape)}")
                owner = None if t.space == "dram" else unit
                self.define(s, scope, name, Sym(t, owner))
            case _:
                self.fail(s, f"no L2 statement {s!r}")

    def define(self, s, scope, name, sym) -> None:
        if name in scope and scope[name].owner is None and sym.owner is not None:
            self.fail(s, f"{name} shadows a memory value")
        scope[name] = sym

    def on(self, s, unit, kind, what) -> None:
        if unit is None or unit[0] != kind:
            self.fail(s, f"{what} runs on {kind}: inside `par .. on {kind}[..]`")

    # --------------------------------------------------------------- ops
    def op(self, s, o: L.Op, scope, loops, unit) -> tuple:
        if o.op == "gemm":
            return self.gemm(s, o, scope, loops, unit)
        if o.op == "drain":
            self.on(s, unit, "mg", "a drain")
            (a,) = o.args or [None]
            sym = scope.get(a.name) if isinstance(a, L.View) else None
            if sym is None or sym.type.space != "acc" or sym.owner != unit:
                self.fail(s, "a drain empties this cluster's accumulator: `drain ACC`")
            return sym.type.shape, "f16"
        self.on(s, unit, "vc", f"`{o.op}`")
        shapes = [self.register(s, scope, a, loops, unit)[0] for a in o.args]
        n = len(o.args)
        if (
            o.op in UNARY
            and n == 1
            or o.op in BINARY
            and n == 2
            or o.op in TERNARY
            and n == 3
        ):
            try:
                return tuple(np.broadcast_shapes(*shapes)), _dtype(o)
            except ValueError:
                self.fail(
                    s, f"{o.op} of {[list(x) for x in shapes]} does not broadcast"
                )
        if o.op in REDUCE and n == 1 and shapes[0]:
            return shapes[0][:-1], _dtype(o)
        if o.op == "quantise" and n == 1:
            return shapes[0], "mx7"
        if o.op == "transpose" and n == 1 and len(shapes[0]) >= 2:
            return shapes[0][:-2] + (shapes[0][-1], shapes[0][-2]), _dtype(o)
        self.fail(s, f"no L2 vector op {o.op} of {n} operands")

    def gemm(self, s, o: L.Op, scope, loops, unit) -> tuple:
        self.on(s, unit, "mg", "a gemm")
        if len(o.args) != 2 or not all(isinstance(a, L.View) for a in o.args):
            self.fail(s, "`gemm A_VIEW, B_VIEW k=K`")
        (sa, a), (sb, b) = (
            (self.memory(s, scope, v), self.view(s, scope, v, loops)) for v in o.args
        )
        if (sa.type.dtype, sb.type.dtype) != ("mx7a", "mx7b"):
            self.fail(s, "a gemm reads an mx7a A and an mx7b B")
        if len(a) != 2 or len(b) != 2 or a[1] != b[1]:
            self.fail(s, f"A {list(a)} and B {list(b)}: [M, K] and [N, K]")
        k = dict(o.attrs).get("k")
        if k != a[1]:
            self.fail(s, f"k={k}, the operands' K is {a[1]}")
        if o.type is None or o.type.space != "acc":
            self.fail(s, "a gemm sums into the accumulator: `: f32[M, N] @acc`")
        return (a[0], b[0]), "f32"

    # ------------------------------------------------------------ values
    def memory(self, s, scope, v: L.View) -> Sym:
        sym = scope.get(v.name)
        if sym is None:
            self.fail(s, f"{v.name} is not defined here")
        if sym.type.space != "dram":
            self.fail(s, f"{v.name} is not in memory (@{sym.type.space})")
        return sym

    def register(self, s, scope, a, loops, unit) -> tuple:
        if isinstance(a, L.Const):
            return (), "const"
        sym = scope.get(a.name)
        if sym is None:
            self.fail(s, f"{a.name} is not defined here")
        if sym.owner != unit:
            where = "memory: load it" if sym.owner is None else "another unit's"
            self.fail(s, f"{a.name} is {where}")
        return self.view(s, scope, a, loops), sym.type.dtype

    def view(self, s, scope, v: L.View, loops) -> tuple:
        dims = scope[v.name].type.shape
        if not v.axes:
            return dims
        if sum(1 for x in v.axes if not x.new) != len(dims):
            self.fail(s, f"{v.name} has {len(dims)} axes")
        for x in v.axes:
            for n in _names(x.lo) - set(loops):
                self.fail(s, f"{n} in a view is not a loop variable here")
        out, d = [], 0
        envs = self.points(loops)
        for x in v.axes:
            if x.new:
                out.append(1)
                continue
            size = dims[d]
            if x.lo is not None:
                span = x.size or 1
                for env in envs:
                    lo = value(x.lo, env)
                    if lo < 0 or lo + span > size:
                        self.fail(
                            s, f"{v.name} axis {d}: {lo}..{lo + span} outside 0..{size}"
                        )
                if x.size is not None:
                    out.append(x.size)
            else:
                out.append(size)
            d += 1
        return tuple(out)

    def points(self, loops) -> list:
        names = list(loops)
        ranges = [range(*loops[n]) for n in names]
        total = 1
        for r in ranges:
            total *= len(r)
        if total > POINTS:
            ranges = [sorted({r[0], r[-1]}) for r in ranges]
        return [dict(zip(names, p, strict=True)) for p in itertools.product(*ranges)]


def _dtype(o: L.Op) -> str:
    return o.type.dtype if o.type is not None else "f32"


__all__ = ["check", "value"]
