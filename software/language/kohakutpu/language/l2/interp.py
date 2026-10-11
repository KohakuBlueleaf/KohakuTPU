"""The L2 reference interpreter: a verified body run in numpy, the meaning an
L2 body has before it is lowered.

Memory is the parameters and buffers, float64 arrays holding their dtype's
values; an `mx7a` / `mx7b` buffer holds MXFP7 values, blocked along its last
axis. Instances run one after another in `par` order, each with registers of
its own; a `for` runs its trips in order (`pipe` is a schedule, not a
meaning). An op computes in float64 (the L3 op table's meaning) and rounds to
its annotated dtype (`f32` is the lane format, E8M15); a gemm's accumulator
holds the exact sum, as L3's `mmt` does before its one rounding (the
cluster's FP22 accumulator stays within a third of an FP16 ulp of it), a drain
rounds it to f16, a store rounds to the destination's dtype. An MXFP7 value
moves as it is: quantised twice it is not itself.
"""

import numpy as np
from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2.verify import value
from kohakutpu.language.l3.ops import OPS

MX7 = {"mx7", "mx7a", "mx7b"}
#: L2 op names the L3 op table spells otherwise.
RENAMED = {"sel": "select"}


def cast(dtype: str, x):
    """`x` rounded to `dtype`'s values."""
    return OPS.dtypes["mx7" if dtype in MX7 else dtype](np.asarray(x, np.float64))


def same(a: str, b: str) -> bool:
    return a == b or (a in MX7 and b in MX7)


class Interp:
    def __init__(self, body: L.Body) -> None:
        self.body = body
        #: name -> [array, dtype]: parameters, buffers, values bound @dram.
        self.memory: dict = {}

    def run(self, inputs: dict) -> dict:
        """Run the body on `inputs` (parameter -> host array, in the L2
        parameter's layout); the memory after, name -> array."""
        for name, t in self.body.params:
            if name in inputs:
                x = np.asarray(inputs[name], np.float64)
                if x.shape != t.shape:
                    raise ValueError(f"{name}: {x.shape}, the body takes {t.shape}")
                if t.dtype in MX7:
                    x = cast("f16", x)
                self.memory[name] = [cast(t.dtype, x), t.dtype]
            else:
                self.memory[name] = [np.zeros(t.shape), t.dtype]
        self.block(self.body.stmts, {}, {})
        return {n: v[0] for n, v in self.memory.items()}

    # ------------------------------------------------------------ blocks
    def block(self, stmts, regs: dict, loops: dict) -> None:
        for s in stmts:
            match s:
                case L.Buffer(name, t):
                    self.memory[name] = [np.zeros(t.shape), t.dtype]
                case L.Par(var, lo, hi, _, _, inner):
                    for i in range(lo, hi):
                        self.block(inner, {}, {**loops, var: i})
                case L.For(var, lo, hi, _, inner):
                    for i in range(lo, hi):
                        self.block(inner, regs, {**loops, var: i})
                case L.Load(name, src, t):
                    x, dtype = self.read(src, {}, loops)
                    regs[name] = (np.array(x), dtype)
                case L.Op(name, _, _, _, t):
                    x = self.op(s, regs, loops)
                    if t.space == "dram":
                        self.memory[name] = [x, t.dtype]
                    else:
                        regs[name] = (x, t.dtype)
                case L.Store(dst, v):
                    self.store(dst, v, regs, loops)
                case _:
                    raise ValueError(f"no L2 statement {s!r}")

    # ------------------------------------------------------------- values
    def index(self, v: L.View, loops: dict) -> tuple:
        out = []
        for a in v.axes:
            if a.new:
                out.append(None)
            elif a.lo is None:
                out.append(slice(None))
            elif a.size is None:
                out.append(value(a.lo, loops))
            else:
                lo = value(a.lo, loops)
                out.append(slice(lo, lo + a.size))
        return tuple(out)

    def read(self, v, regs: dict, loops: dict) -> tuple:
        """``(array, dtype)`` of a view of a register or of memory."""
        if v.name in regs:
            x, dtype = regs[v.name]
        else:
            x, dtype = self.memory[v.name]
        return (x[self.index(v, loops)] if v.axes else x), dtype

    def operand(self, a, regs: dict, loops: dict):
        if isinstance(a, L.Const):
            return np.float64(a.value)
        return self.read(a, regs, loops)[0]

    def op(self, o: L.Op, regs: dict, loops: dict, shape=None):
        """`o`'s value, rounded to its dtype; `shape` broadcasts an inline
        store's value to the view it is stored into."""
        if o.op == "gemm":
            a, b = (self.read(x, regs, loops)[0] for x in o.args)
            return a @ b.T
        xs = [self.operand(a, regs, loops) for a in o.args]
        if o.op == "drain":
            return cast("f16", xs[0])
        if o.op == "quantise":
            x = xs[0] if shape is None else np.broadcast_to(xs[0], shape)
            return cast("mx7", x)
        dtype = o.type.dtype if o.type is not None else "f32"
        if o.op == "transpose":
            return cast(dtype, np.swapaxes(xs[0], -1, -2))
        if o.op.startswith("reduce."):
            out = np.shape(xs[0])[:-1]
        else:
            out = np.broadcast_shapes(*(np.shape(x) for x in xs))
            if shape is not None:
                out = np.broadcast_shapes(out, shape)
        return cast(dtype, OPS.ops[RENAMED.get(o.op, o.op)].run(xs, dict(o.attrs), out))

    def store(self, dst: L.View, v, regs: dict, loops: dict) -> None:
        mem, dtype = self.memory[dst.name]
        at = self.index(dst, loops) if dst.axes else ()
        shape = mem[at].shape
        if isinstance(v, L.Op):
            x = self.op(v, regs, loops, shape)
            src = "mx7" if v.op == "quantise" else ("f16" if v.op == "drain" else None)
        else:
            x, src = self.read(v, regs, loops)
        x = np.broadcast_to(x, shape)
        mem[at] = x if src is not None and same(src, dtype) else cast(dtype, x)


def run(body: L.Body, inputs: dict) -> dict:
    """`body` on `inputs` (parameter -> host array); memory after."""
    return Interp(body).run(inputs)


__all__ = ["Interp", "cast", "run"]
