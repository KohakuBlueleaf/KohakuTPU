"""The L3 reference interpreter: a verified module run in numpy.

Every op computes in float64 and its result is rounded to the dtype its
statement names (`OpSet.dtypes`), the value the hardware would store. A value
moved into a carry, a store or a parameter of another dtype is rounded to that
one; of the same dtype it is moved as it is (MXFP7 rounded twice is not
itself). A map's iterations are independent and run in order.
"""

import itertools

import numpy as np
from kohakuaccel.ir.l3 import nodes as N


class Env:
    """Names in scope: values with their dtypes, dims, loop variables."""

    def __init__(self, parent=None) -> None:
        self.parent, self.names, self.dtypes = parent, {}, {}

    def get(self, name):
        e = self
        while e is not None:
            if name in e.names:
                return e.names[name]
            e = e.parent
        raise KeyError(name)

    def dtype(self, name):
        e = self
        while e is not None:
            if name in e.names:
                return e.dtypes.get(name)
            e = e.parent
        raise KeyError(name)

    def bind(self, name, value, dtype=None) -> None:
        self.names[name] = value
        if dtype is not None:
            self.dtypes[name] = dtype


class _Loop:
    """A loop variable's value: point `k`, or tile `k` of `size` points."""

    def __init__(self, k: int, size: int | None) -> None:
        self.k, self.size = k, size


class _Interp:
    def __init__(self, module: N.Module, ops) -> None:
        self.module, self.ops = module, ops
        #: output name -> (array, dtype)
        self.outputs: dict = {}

    def cast(self, dtype: str, x, src: str | None = None):
        if src == dtype:
            return np.asarray(x)
        return self.ops.dtypes[dtype](np.asarray(x, np.float64))

    def dim(self, env, d) -> int:
        return d if isinstance(d, int) else env.get(d)

    def shape(self, env, t: N.Type) -> tuple:
        return tuple(self.dim(env, d) for d in t.shape)

    def operand(self, env, x) -> tuple:
        """``(value, dtype)``; a constant's dtype is None."""
        match x:
            case N.Const(v):
                return np.float64(v), None
            case N.Ref(name):
                v = env.get(name)
                if isinstance(v, int):
                    return np.float64(v), None
                return v, env.dtype(name)
            case N.Index(name, entries):
                return env.get(name)[self.index(env, entries)], env.dtype(name)
        raise TypeError(x)

    def index(self, env, entries) -> tuple:
        out = []
        for en in entries:
            match en:
                case N.Full():
                    out.append(slice(None))
                case N.NewAxis():
                    out.append(None)
                case N.At(int() as i):
                    out.append(i)
                case N.At(str() as v):
                    loop = env.get(v)
                    if loop.size is None:
                        out.append(loop.k)
                    else:
                        out.append(slice(loop.k * loop.size, (loop.k + 1) * loop.size))
                case N.Range(lo, hi):
                    out.append(slice(self.dim(env, lo), self.dim(env, hi)))
        return tuple(out)

    def points(self, env, d) -> list:
        match d:
            case N.Span(lo, hi):
                return [
                    _Loop(k, None) for k in range(self.dim(env, lo), self.dim(env, hi))
                ]
            case N.Tiles(extent, size):
                ext, sz = self.dim(env, extent), self.dim(env, size)
                if ext % sz:
                    raise ValueError(f"{ext} is not whole tiles of {sz}")
                return [_Loop(k, sz) for k in range(ext // sz)]
        raise TypeError(d)

    def block(self, env, body, nexts=None):
        carries = []
        for s in body:
            match s:
                case N.Tile(name, value):
                    env.bind(name, value)
                case N.Output(name, t):
                    out = np.zeros(self.shape(env, t))
                    env.bind(name, out, t.dtype)
                    self.outputs[name] = (out, t.dtype)
                case N.Let(name, op, args, attrs, t):
                    shape = self.shape(env, t)
                    xs = [self.operand(env, a)[0] for a in args]
                    v = self.cast(t.dtype, self.ops.ops[op].run(xs, dict(attrs), shape))
                    if v.shape != shape:
                        raise ValueError(f"{name} = {op}: {v.shape}, declared {shape}")
                    env.bind(name, v, t.dtype)
                case N.Invoke(name, fn, args, _, _t):
                    f = self.module.fns[fn]
                    env.bind(
                        name,
                        self.call(f, [self.operand(env, a) for a in args]),
                        f.ret.dtype,
                    )
                case N.Carry(name, init, t):
                    full = np.full(self.shape(env, t), init.value)
                    env.bind(name, self.cast(t.dtype, full), t.dtype)
                    carries.append(s)
                case N.Map(vars, inner):
                    domains = [self.points(env, d) for _, d in vars]
                    for point in itertools.product(*domains):
                        sub = Env(env)
                        for (v, _), loop in zip(vars, point, strict=True):
                            sub.bind(v, loop)
                        self.block(sub, inner)
                case N.Scan(var, d, inner):
                    for loop in self.points(env, d):
                        sub = Env(env)
                        sub.bind(var, loop)
                        upd: dict = {}
                        self.block(sub, inner, upd)
                        for c in carries:
                            v, src = upd[c.name]
                            v = np.broadcast_to(v, env.names[c.name].shape)
                            env.bind(
                                c.name, self.cast(c.type.dtype, v, src), c.type.dtype
                            )
                    carries = []
                case N.Next(name, value):
                    nexts[name] = self.operand(env, value)
                case N.Store(target, value):
                    out, dtype = self.outputs[target.name]
                    v, src = self.operand(env, value)
                    out[self.index(env, target.entries)] = self.cast(dtype, v, src)
                case N.Return(value):
                    return self.operand(env, value)[0]
        return None

    def call(self, fn: N.Fn, args):
        env = Env()
        for (n, t), (a, src) in zip(fn.params, args, strict=True):
            a = self.cast(t.dtype, a, src)
            for want, have in zip(t.shape, a.shape, strict=True):
                if isinstance(want, str):
                    env.bind(want, have)
            env.bind(n, a, t.dtype)
        return self.block(env, fn.body)

    def program(self, p: N.Program, inputs: dict) -> dict:
        env = Env()
        for n, t in p.params:
            if n not in inputs:
                raise ValueError(f"{p.name} wants {n}")
            a = np.asarray(inputs[n])
            if len(a.shape) != len(t.shape):
                raise ValueError(f"{n}: rank {a.ndim} for {len(t.shape)}")
            for want, have in zip(t.shape, a.shape, strict=True):
                if isinstance(want, int) and want != have:
                    raise ValueError(f"{n}: {a.shape} for {t.shape}")
                if isinstance(want, str) and env.names.setdefault(want, have) != have:
                    raise ValueError(f"{n}: {want} is {env.names[want]} and {have}")
            env.bind(n, self.cast(t.dtype, a), t.dtype)
        self.block(env, p.body)
        return {n: v for n, (v, _) in self.outputs.items()}


def run(module: N.Module, ops, program: str, inputs: dict) -> dict:
    """The outputs of `program` on `inputs` (name -> array), each float64
    holding its dtype's values."""
    return _Interp(module, ops).program(module.programs[program], inputs)


__all__ = ["run"]
