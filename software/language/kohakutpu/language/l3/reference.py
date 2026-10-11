"""An `fn NAME.l3` body run by the numpy reference interpreter: the reference
every lower body of the kernel is checked against, and the work it states."""

from kohakutpu.language.l3 import nodes as N
from kohakutpu.language.l3 import reader
from kohakutpu.language.l3.interp import Exact, Interp
from kohakutpu.language.l3.ops import OPS


def run(ktpu, name: str, inputs: list, exact: bool = False):
    """`name.l3` on `inputs` (arrays, in parameter order): float64 holding
    the result dtype's values; `exact` runs it with no rounding at all."""
    m = reader.module(ktpu)
    args = [(x, None) for x in inputs]
    return (Exact if exact else Interp)(m, OPS).call(m.fns[name], args)


def work(ktpu, name: str, shapes: list) -> dict:
    """The work `name.l3` states for inputs of `shapes`: ``lane_ops`` (one a
    vector-lane element an op reads or writes, the larger) and ``macs`` (the
    clusters' multiply-adds) -- the numerators of lane use and MFU."""
    fn = reader.module(ktpu).fns[name]
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


__all__ = ["run", "work"]
