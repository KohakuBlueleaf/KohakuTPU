"""An `fn NAME.l3` body as the L3 IR (`kohakuaccel.ir.l3`): read with L3's
own statement reader, verified against KohakuTPU's ops, and run by the numpy
reference interpreter -- the reference every lower body of the kernel is
checked against."""

import numpy as np
from kohakuaccel.ir.l3 import nodes as N
from kohakuaccel.ir.l3.interp import _Interp
from kohakuaccel.ir.l3.text import L3Reader, _read_stmt
from kohakuaccel.ir.l3.verify import verify
from kohakuaccel.text.syntax import Stmt
from kohakutpu.ir.l3.ops import OPS


def l3_module(module) -> N.Module:
    """Every `fn NAME.l3` of a `.ktpu` module, as one verified L3 module."""
    r = L3Reader(module.source, module.file)
    out = N.Module()
    for (name, level), body in module.fns.items():
        if level != "l3":
            continue
        at = Stmt("fn", line=body.line, col=1)
        if body.ret is None:
            r.fail(at, f"{name}.l3 returns its result: `-> TYPE`")
        params = tuple((n, r.type(at, t)) for n, t in body.params)
        stmts = tuple(_read_stmt(r, s) for s in body.stmts)
        out.fns[name] = r.at(at, N.Fn(name, params, r.type(at, body.ret), stmts))
    out.positions = r.positions
    verify(out, OPS, module.source, module.file)
    return out


class _Exact(_Interp):
    """Every dtype cast is the identity: the body's math in float64, no fp16
    rounding and no MXFP7 quantisation."""

    def cast(self, dtype: str, x, src: str | None = None):
        return np.asarray(x, np.float64)


def run(module, name: str, inputs: list, exact: bool = False):
    """`name.l3` on `inputs` (arrays, in parameter order): float64 holding
    the result dtype's values; `exact` runs it with no rounding at all."""
    m = l3_module(module)
    fn = m.fns[name]
    args = [(x, None) for x in inputs]
    return (_Exact if exact else _Interp)(m, OPS).call(fn, args)


def work(module, name: str, shapes: list) -> dict:
    """The work `name.l3` states for inputs of `shapes`: ``lane_ops`` (one a
    vector-lane element an op reads or writes, the larger) and ``macs`` (the
    clusters' multiply-adds) -- the numerators of lane use and MFU."""
    fn = l3_module(module).fns[name]
    dims: dict = {}
    for (_, t), shape in zip(fn.params, shapes, strict=True):
        for d, n in zip(t.shape, shape, strict=True):
            if isinstance(d, str):
                dims[d] = n
    sizes = {n: _size(t, dims) for n, t in fn.params}
    out = {"lane_ops": 0, "macs": 0}
    _count(fn.body, dims, sizes, 1, out)
    return out


def _size(t, dims) -> int:
    n = 1
    for d in t.shape:
        n *= d if isinstance(d, int) else dims[d]
    return n


def _count(body, dims, sizes, trips, out) -> None:
    for s in body:
        match s:
            case N.Tile(name, value):
                dims[name] = value
            case N.Let(name, op, args, _, t):
                size = _size(t, dims)
                sizes[name] = size
                reads = [sizes.get(a.name, 1) for a in args if hasattr(a, "name")]
                if op == "mmt":
                    a, b = (x.name for x in args)
                    out["macs"] += trips * _mmt_macs(sizes[a], size, sizes[b])
                else:
                    out["lane_ops"] += trips * max([size, *reads])
            case N.Map(vars, inner):
                n = 1
                for _, d in vars:
                    n *= _trips(d, dims)
                _count(inner, dims, sizes, trips * n, out)
            case N.Scan(_, d, inner):
                _count(inner, dims, sizes, trips * _trips(d, dims), out)


def _mmt_macs(a: int, c: int, b: int) -> int:
    """MACs of C[M, N] = A[M, K] B[N, K]^T from the three sizes: M*N*K with
    K = sqrt(a * b / c)."""
    return round((a * b * c) ** 0.5)


def _trips(d, dims) -> int:
    def v(x):
        return x if isinstance(x, int) else dims[x]

    if isinstance(d, N.Span):
        return v(d.hi) - v(d.lo)
    return v(d.extent) // v(d.size)


__all__ = ["l3_module", "run", "work"]
