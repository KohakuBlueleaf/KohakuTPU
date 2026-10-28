"""A plan's meaning in numpy: its output over its kept ranges, every read
taken at its strides, each reduction over its own ranges, in float64. The
plan rewrites (`window.tabled`, ...) are checked against it."""

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.storage import FLAT
from kohakutpu.language.numerics import fp16


def evaluate(plan: Plan, buffers: dict) -> np.ndarray:
    """`plan`'s output, shaped by its kept ranges, with each buffer read
    from ``buffers[slot]`` (flat). A read outside its buffer is NaN (a `where`
    that does not take it drops it)."""
    reduced = [r for rs in plan.reductions.values() for r in rs]
    if plan.kind == "matmul":
        reduced += list(plan.mnk[2])
    grid = list(dict.fromkeys([*plan.out_ranges, *plan.reduce_ranges, *reduced]))
    axis = {r: i for i, r in enumerate(grid)}
    shape = [U.extent(r) for r in grid]

    def read(acc: Access) -> np.ndarray:
        flat = np.asarray(
            buffers[acc.slot] if acc.slot is not None else acc.table, np.float64
        ).reshape(-1)
        idx = np.full([1] * len(grid), acc.off, np.int64)
        for r, c in acc.coef:
            at = [1] * len(grid)
            at[axis[r]] = U.extent(r)
            idx = idx + c * np.arange(U.extent(r)).reshape(at)
        inside = (idx >= 0) & (idx < flat.size)
        return np.where(inside, flat[np.clip(idx, 0, max(flat.size - 1, 0))], np.nan)

    def fold(e, loads):
        if isinstance(e, X.Load):
            return loads(e.i)
        if isinstance(e, X.Const):
            return np.float64(e.value)
        if isinstance(e, X.Reduce):
            x = np.broadcast_to(fold(e.arg, loads), shape)
            over = tuple(axis[r] for r in plan.reductions[id(e)])
            f = {"sum": np.sum, "max": np.max, "prod": np.prod}[e.op]
            return f(x, axis=over, keepdims=True)
        args = [fold(a, loads) for a in e.args]
        op = X.Op(e.name, tuple(X.Load(i) for i in range(len(args))))
        return X.evaluate(op, args)

    if plan.kind == "matmul":
        assert plan.a is not None and plan.b is not None
        ks = tuple(axis[r] for r in plan.mnk[2])
        prod = np.sum(
            np.broadcast_to(read(plan.a) * read(plan.b), shape), axis=ks, keepdims=True
        )
        out = fold(plan.expr, lambda i: prod if i == 0 else read(plan.accesses[i - 1]))
    else:
        e = X.Load(0) if plan.expr is None else plan.expr
        out = fold(e, lambda i: read(plan.accesses[i]))
    out = np.broadcast_to(out, shape)
    keep = len(plan.out_ranges)
    out = out[(slice(None),) * keep + (0,) * (len(grid) - keep)]
    return np.ascontiguousarray(out).reshape(plan.out_shape)


def execute(dev, plan: Plan, bufs: dict) -> list:
    """`run.execute`'s contract on `dev`'s memory, computed by `evaluate`:
    the reads' bytes read back, the output's written (into its window), no
    kernel run. A reference for the device's runs."""
    if plan.kind == "const":
        out = np.asarray(plan.value, np.float64).reshape(plan.out_shape)
    else:
        arrays: dict = {}
        for a in [*plan.accesses, plan.a, plan.b]:
            if a is None or a.slot is None or a.slot in arrays:
                continue
            view = bufs[a.slot]
            st, off = FM.base_of(view)
            dt = np.dtype(F.np_dtype(a.dtype))
            n = view.nbytes // dt.itemsize
            raw = dev.read(st.addr + off, n * dt.itemsize)
            arrays[a.slot] = np.frombuffer(raw, dt, n)
        out = evaluate(plan, arrays)
    view = bufs[plan.out_slot]
    st, off = FM.base_of(view)
    dt = np.dtype(F.np_dtype(plan.out_dtype))
    strides = plan.out_strides or _dense([U.extent(r) for r in plan.out_ranges])
    pos = np.zeros(plan.out_shape, np.int64)
    for i, (n, s) in enumerate(zip(plan.out_shape, strides)):
        at = [1] * len(plan.out_shape)
        at[i] = n
        pos = pos + s * np.arange(n).reshape(at)
    out = fp16.write(out) if dt == np.float16 else out.astype(dt)
    _patch(dev, st.addr + off, pos.reshape(-1), out.reshape(-1))
    st.wrote(FLAT)
    st.dtype = dt
    return []


def _dense(extents) -> list:
    out, step = [], 1
    for n in reversed(extents):
        out.insert(0, step)
        step *= n
    return out


def _patch(dev, addr: int, pos: np.ndarray, vals: np.ndarray) -> None:
    """`vals` at elements `pos` from `addr`, every other byte kept: the
    whole lines the span covers read, patched, written back."""
    item = vals.dtype.itemsize
    line = dev.card.line
    lo = addr + item * int(pos.min()) if pos.size else addr
    hi = addr + item * (int(pos.max()) + 1) if pos.size else addr
    a, b = lo - lo % line, -(-hi // line) * line
    buf = np.frombuffer(bytearray(dev.read(a, b - a)), np.uint8).copy()
    at = (addr - a) + item * pos
    view = vals.view(np.uint8).reshape(-1, item)
    for k in range(item):
        buf[at + k] = view[:, k]
    dev.write(a, buf.tobytes())


__all__ = ["evaluate", "execute"]
