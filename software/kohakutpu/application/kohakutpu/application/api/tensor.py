"""`Tensor`: an array on the card. Ops build a lazy expression; realising it
runs ONE generated DSL kernel (rows, reduction or contraction with an
epilogue). With fusion on (`set_fusion`), an elementwise chain, the row
statistics it reads within one column block, and the epilogue of a product
are folded into that kernel; a part no kernel covers is realised first.

Physical forms follow the runtime (`runtime.storage`): row-major f16 bytes,
or a row statistic held as [R], 16 f16 values a word (shown with the
reduced axis dropped or kept as 1). Anything no generated kernel covers is
refused by clause (`Unsupported`); nothing runs on the host.
"""

import contextlib
from dataclasses import dataclass

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import AxisType, UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.lower import Access, Plan, Refused
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.call import upload
from kohakutpu.application.runtime.storage import FLAT, Storage
from kohakutpu.language.numerics import fp16

_fuse = True


def set_fusion(on: bool) -> bool:
    """Fold chains into one kernel (True) or run every op as its own kernel
    (False); returns the previous setting."""
    global _fuse
    old, _fuse = _fuse, bool(on)
    return old


class Unsupported(RuntimeError):
    """An unsupported API operation, identified by its spec clause."""

    def __init__(self, clause: str, message: str):
        self.clause = clause
        super().__init__(f"{clause}: {message}")


class Buf:
    """A storage shared by the tensors viewing it; freed with the last."""

    def __init__(self, st: Storage, dtype) -> None:
        self.st, self.dtype = st, np.dtype(dtype)

    def __del__(self) -> None:
        # At interpreter shutdown the device may be gone before its tensors.
        with contextlib.suppress(Exception):
            self.st.free()


@dataclass(frozen=True, eq=False)
class Ref:
    """A lazy expression's read of a realised tensor."""

    t: "Tensor"


@dataclass(frozen=True, eq=False)
class Prod:
    """A lazy expression's read of the product ``a @ b.T``."""

    a: "Tensor"
    b: "Tensor"


@dataclass(eq=False)
class Node:
    """What a lazy tensor computes: `expr` (X ops over `Ref` / `Prod`
    leaves) on the rows view `view` its reads classify against."""

    expr: object
    view: tuple


def _op(name, *args):
    return X.Op(name, tuple(args))


def _key(x) -> int:
    """One read a tensor, however many `Ref`s name it."""
    return id(x.t) if isinstance(x, Ref) else id(x)


def _leaves(e, out=None) -> list:
    out = [] if out is None else out
    if isinstance(e, (Ref, Prod)):
        if not any(_key(x) == _key(e) for x in out):
            out.append(e)
    elif isinstance(e, X.Op):
        for a in e.args:
            _leaves(a, out)
    elif isinstance(e, X.Reduce):
        _leaves(e.arg, out)
    return out


def _prod_of(e):
    return next((x for x in _leaves(e) if isinstance(x, Prod)), None)


class Tensor:
    """An f16 (or f32) array on the device; see the module docstring."""

    __array_ufunc__ = None  # `ndarray * Tensor` defers to Tensor

    def __init__(
        self,
        shape,
        buf: Buf | None = None,
        node: Node | None = None,
        stat=False,
        T=None,
    ):
        self.shape = tuple(int(d) for d in shape)
        self._buf, self._node, self._stat = buf, node, stat
        self._T = T  # the [N, K] tensor this is the transpose view of

    # ------------------------------------------------------------ facts
    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        return int(np.prod(self.shape))

    @property
    def dtype(self):
        return self._buf.dtype if self._buf is not None else np.dtype(np.float16)

    @property
    def lazy(self) -> bool:
        return self._node is not None

    @property
    def st(self) -> Storage:
        """The storage of a realised tensor."""
        if self._buf is None:
            raise Unsupported(
                "API.REF.008", "a lazy tensor has no storage yet: realize() it"
            )
        return self._buf.st

    @property
    def rows(self) -> tuple:
        """The [.., W] view a kernel reads: a statistic's ends in 1."""
        if self._stat and self.shape[-1:] != (1,):
            return self.shape + (1,)
        return self.shape or (1,)

    def __repr__(self) -> str:
        state = "lazy" if self.lazy else "statistic" if self._stat else "flat"
        return f"Tensor({list(self.shape)}, {self.dtype.name}, {state})"

    # ---------------------------------------------------------- realise
    def realize(self) -> "Tensor":
        if self._node is not None:
            _realize(self)
        return self

    def numpy(self) -> np.ndarray:
        self.realize()
        if self._T is not None:
            return self._T.numpy().swapaxes(-1, -2)
        dev, st = D.device(), self.st
        if self._stat:
            raw = FM.read(dev, st, np.float16, max(1, self.size))
            return np.frombuffer(raw, np.float16).reshape(self.shape).copy()
        raw = FM.read(dev, st, self.dtype, self.size)
        return np.frombuffer(raw, self.dtype).reshape(self.shape).copy()

    def reshape(self, *shape) -> "Tensor":
        shape = (
            shape[0]
            if len(shape) == 1 and isinstance(shape[0], (tuple, list))
            else shape
        )
        shape = tuple(np.empty(self.shape, np.int8).reshape(shape).shape)
        if self._T is not None:
            raise Unsupported(
                "API.REF.009", "a reshape of a transposed view: only matmul reads one"
            )
        self.realize()
        return Tensor(shape, self._buf, stat=self._stat)

    def flatten(self) -> "Tensor":
        return self.reshape(self.size)

    @property
    def T(self) -> "Tensor":
        """The transpose of the last two axes, as matmul's right operand
        (the clusters read B as [N, K]): no bytes move."""
        if self.ndim < 2:
            raise Unsupported(
                "API.REF.009", f"a transpose of a rank-{self.ndim} tensor"
            )
        if self._T is not None:
            return self._T
        self.realize()
        return Tensor(
            self.shape[:-2] + (self.shape[-1], self.shape[-2]), self._buf, T=self
        )

    # ------------------------------------------------------ elementwise
    def __add__(self, o):
        return _ew("add", self, o)

    def __radd__(self, o):
        return _ew("add", o, self)

    def __sub__(self, o):
        return _ew("sub", self, o)

    def __rsub__(self, o):
        return _ew("sub", o, self)

    def __mul__(self, o):
        return _ew("mul", self, o)

    def __rmul__(self, o):
        return _ew("mul", o, self)

    def __truediv__(self, o):
        return _ew("div", self, o)

    def __rtruediv__(self, o):
        return _ew("div", o, self)

    def __pow__(self, o):
        if isinstance(o, (int, float)) and float(o) == 2.0:
            return self * self
        return _ew("pow", self, o)

    def __neg__(self):
        return _ew("neg", self)

    def __abs__(self):
        return _ew("abs", self)

    def __lt__(self, o):
        return _ew("cmplt", self, o)

    def __gt__(self, o):
        return _ew("cmpgt", self, o)

    def __le__(self, o):
        return _ew("not", _ew("cmpgt", self, o))

    def __ge__(self, o):
        return _ew("not", _ew("cmplt", self, o))

    def eq(self, o):
        return _ew("cmpeq", self, o)

    def ne(self, o):
        return _ew("cmpne", self, o)

    def __matmul__(self, o):
        return matmul(self, o)

    def __bool__(self):
        raise Unsupported(
            "API.REF.010", "the truth of a device tensor: read it with .numpy() first"
        )

    def maximum(self, o):
        return _ew("max", self, o)

    def minimum(self, o):
        return _ew("min", self, o)

    def where(self, a, b):
        """`a` where self is nonzero, else `b`."""
        return _ew("where", self, a, b)

    def exp2(self):
        return _ew("exp2", self)

    def log2(self):
        return _ew("log2", self)

    def exp(self):
        return _ew("exp2", self * float(np.log2(np.e)))

    def log(self):
        return _ew("log2", self) * float(np.log(2.0))

    def sqrt(self):
        return _ew("sqrt", self)

    def rsqrt(self):
        return _ew("rsqrt", self)

    def reciprocal(self):
        return _ew("recip", self)

    def sin(self):
        return _ew("sin", self)

    def cos(self):
        return (self + float(np.pi / 2)).sin()

    def trunc(self):
        return _ew("trunc", self)

    def square(self):
        return self * self

    def relu(self):
        return _ew("max", self, 0.0)

    def sigmoid(self):
        return ((self * -float(np.log2(np.e))).exp2() + 1.0).reciprocal()

    def tanh(self):
        return (self * 2.0).sigmoid() * 2.0 - 1.0

    def silu(self):
        return self * self.sigmoid()

    def gelu(self):
        """The tanh form, as torch's ``approximate="tanh"``."""
        inner = (self + self * self * self * 0.044715) * float(np.sqrt(2 / np.pi))
        return self * 0.5 * (inner.tanh() + 1.0)

    def clip(self, lo, hi):
        return self.maximum(lo).minimum(hi)

    # -------------------------------------------------------- reductions
    def sum(self, axis=None, keepdim=False):
        return _reduce("sum", self, axis, keepdim)

    def max(self, axis=None, keepdim=False):
        return _reduce("max", self, axis, keepdim)

    def min(self, axis=None, keepdim=False):
        return -_reduce("max", -self, axis, keepdim)

    def prod(self, axis=None, keepdim=False):
        return _reduce("prod", self, axis, keepdim)

    def mean(self, axis=None, keepdim=False):
        n = self.size if axis is None else self.shape[axis]
        return _reduce("sum", self, axis, keepdim) * (1.0 / n)

    def var(self, axis=None, keepdim=False):
        d = (
            self - self.mean(axis, keepdim=True)
            if axis is not None
            else self - self.mean()
        )
        return (d * d).mean(axis, keepdim)

    def std(self, axis=None, keepdim=False):
        return self.var(axis, keepdim).sqrt()

    def softmax(self, axis=-1):
        _last(self, axis, "softmax")
        e = (self - self.max(-1, keepdim=True)).exp()
        return e / e.sum(-1, keepdim=True)

    def log_softmax(self, axis=-1):
        _last(self, axis, "log_softmax")
        s = self - self.max(-1, keepdim=True)
        return s - s.exp().sum(-1, keepdim=True).log()

    def layernorm(self, weight=None, bias=None, eps=1e-5):
        d = self - self.mean(-1, keepdim=True)
        y = d * ((d * d).mean(-1, keepdim=True) + eps).rsqrt()
        y = y if weight is None else y * weight
        return y if bias is None else y + bias

    def rmsnorm(self, weight=None, eps=1e-6):
        y = self * ((self * self).mean(-1, keepdim=True) + eps).rsqrt()
        return y if weight is None else y * weight


# ------------------------------------------------------------- building
def _last(t: Tensor, axis, what: str) -> None:
    if axis not in (-1, t.ndim - 1):
        raise Unsupported(
            "API.REF.002",
            f"{what} over axis {axis} of {t.shape}: rows are the last axis",
        )


def tensor(a, dtype=np.float16) -> Tensor:
    """`a` uploaded as a device tensor (f16 unless `dtype` says f32)."""
    a = np.ascontiguousarray(
        fp16.write(a) if np.dtype(dtype) == np.float16 else a, dtype
    )
    if a.dtype not in (np.float16, np.float32):
        raise Unsupported(
            "API.REF.011", f"a {a.dtype} tensor: the card computes on f16 / f32"
        )
    return Tensor(a.shape, Buf(upload(D.device(), a), a.dtype))


def _check_flat(t: Tensor, why: str) -> None:
    if t._T is not None:
        raise Unsupported(
            "API.REF.005",
            f"{why}: a transposed view is read only as matmul's right operand",
        )


def _fusable(a: Tensor, view: tuple, wide: bool, have_prod, have_red: int) -> bool:
    """May lazy `a` be inlined into an expression over `view`?"""
    if not _fuse or a._node is None or a._node.view != view:
        return False
    # Partial columns need a separate masked reduction before its broadcast.
    if view[-1] % 32 and X.reduces(a._node.expr):
        return False
    e = a._node.expr
    reds = len(X.reduces(e))
    prod = _prod_of(e)
    if prod is not None and (
        (have_prod is not None and have_prod is not prod) or have_red
    ):
        return False
    if (prod is not None or have_prod is not None) and reds:
        return False
    # Past one block, partials fold one reduction, into a statistic.
    return not (reds and view[-1] > V.MAX_BLOCK and (wide or have_red + reds > 1))


def _leaf(a, view: tuple, wide: bool, state: dict):
    if not isinstance(a, Tensor):
        return X.Const(float(a))
    _check_flat(a, "an elementwise operand")
    if a._node is not None and _fusable(a, view, wide, state["prod"], state["red"]):
        e = a._node.expr
        state["red"] += len(X.reduces(e))
        state["prod"] = state["prod"] or _prod_of(e)
        return e
    a.realize()
    return Ref(a)


def _ew(name: str, *args) -> Tensor:
    ts = [a for a in args if isinstance(a, Tensor)]
    try:
        shape = tuple(np.broadcast_shapes(*(t.shape for t in ts)))
    except ValueError:
        raise Unsupported(
            "API.REF.012",
            f"{name} of shapes {[t.shape for t in ts]}: they do not broadcast",
        ) from None
    stat = all(t._stat for t in ts)
    for t in ts:
        if not stat and t._stat and t.size > 1 and t.shape[-1:] != (1,):
            raise Unsupported(
                "API.REF.003",
                f"a row statistic {t.shape} broadcast as a row of values against "
                f"{shape}: keep the reduced axis (keepdim=True)",
            )
    probe = Tensor(shape, stat=stat)
    view = probe.rows
    if stat:
        # Statistics of a lazy reduction: stay in its row view.
        for t in ts:
            if (
                t._node is not None
                and t._node.view[:-1] == view[:-1]
                and t._node.view[-1] != 1
            ):
                view = t._node.view
                break
    state = {"prod": None, "red": 0}
    wide = not stat
    leaves = [_leaf(a, view, wide, state) for a in args]
    return Tensor(shape, node=Node(_op(name, *leaves), view), stat=stat)


def _reduce(op: str, t: Tensor, axis, keepdim: bool) -> Tensor:
    _check_flat(t, f"a {op}")
    if axis is None:
        if t._stat:
            raise Unsupported(
                "API.REF.006",
                f"a {op} over every row statistic: reduce the rows themselves",
            )
        r = _reduce(op, t.realize().reshape(1, t.size), -1, True)
        return Tensor((1,) * t.ndim if keepdim else (), node=r._node, stat=True)
    _last(t, axis, f"a {op}")
    if t._stat:
        raise Unsupported(
            "API.REF.006",
            f"a {op} along a row statistic's rows: reduce the rows themselves",
        )
    view = t.rows
    state = {"prod": None, "red": 1}  # this reduction
    leaf = _leaf(t, view, False, state)
    if state["prod"] is not None:
        t.realize()
        leaf = Ref(t)
    shape = t.shape[:-1] + ((1,) if keepdim else ())
    return Tensor(shape, node=Node(X.Reduce(op, leaf), view), stat=True)


def matmul(a: Tensor, b: Tensor) -> Tensor:
    """``a @ b``: b must be the transpose view ``w.T`` of an [N, K] (or
    batched [.., N, K]) tensor, as a Linear weight is stored."""
    if b._T is None:
        raise Unsupported(
            "API.REF.001",
            "x @ w with w as [K, N]: the clusters read B as [N, K] rows; store the "
            "weight [N, K] and write x @ w.T",
        )
    w = b._T
    if a.ndim < 2 or a.shape[-1] != w.shape[-1]:
        raise Unsupported(
            "API.REF.004",
            f"matmul of {a.shape} and {b.shape}: K differs or A is not 2-D",
        )
    if w.ndim > 2 and w.shape[:-2] != a.shape[:-2]:
        raise Unsupported(
            "API.REF.004", f"batched B {w.shape} for A {a.shape}: one B, or one a batch"
        )
    _check_flat(a, "matmul's left operand")
    a.realize()
    shape = a.shape[:-1] + (w.shape[-2],)
    return Tensor(shape, node=Node(Prod(a, w), shape))


def linear(x: Tensor, w: Tensor, b: Tensor | None = None) -> Tensor:
    """``x @ w.T + b`` (w [N, K]): one contraction with its epilogue."""
    y = matmul(x, w.T)
    return y if b is None else y + b


# ------------------------------------------------------------ realising
def _kind(t: Tensor, view: tuple) -> str:
    """How a kernel over `view` reads `t`: whole rows, one row every row
    reads, a value a row, one value."""
    s = t.rows
    if t.size == 1:
        return "scalar"
    s = (1,) * (len(view) - len(s)) + s
    if len(s) != len(view):
        raise Unsupported("API.REF.013", f"a {t.shape} operand of a {view} kernel")
    if s == view:
        return "full"
    if s[-1] == view[-1] and all(d == 1 for d in s[:-1]):
        return "col"
    if s[-1] == 1 and s[:-1] == view[:-1]:
        return "row"
    raise Unsupported(
        "API.REF.013",
        f"a {t.shape} operand broadcast to {view}: kernels read whole rows, one row, "
        "a value a row or one value",
    )


def _addr(dev, t: Tensor, kind: str, stat_space: bool) -> tuple:
    st = t.st
    R = RW.rows_of(t.rows)
    if stat_space and kind != "scalar":
        # Statistics of statistics: each value a row.
        return FM.f16(dev, st, t.dtype, R), Operand("row", rows=R)
    if kind in ("row", "scalar"):
        return FM.f16(dev, st, t.dtype, max(1, t.size)), Operand(kind)
    if t._stat:
        raise Unsupported(
            "API.REF.003",
            f"a row statistic {t.shape} read as a row of values: keep the reduced "
            "axis (keepdim=True) so it broadcasts down the rows",
        )
    return FM.f16(dev, st, t.dtype, t.size), Operand(
        kind, pitch=t.shape[-1] if kind == "full" else 0
    )


def _lower(e, leaves: list, memo: dict):
    """`e` with each leaf as ``Load(its index in leaves)`` (shared
    subexpressions stay shared)."""
    got = memo.get(id(e))
    if got is not None:
        return got
    if isinstance(e, (Ref, Prod)):
        out = X.Load(next(i for i, x in enumerate(leaves) if _key(x) == _key(e)))
    elif isinstance(e, X.Op):
        out = X.Op(e.name, tuple(_lower(a, leaves, memo) for a in e.args))
    elif isinstance(e, X.Reduce):
        out = X.Reduce(e.op, _lower(e.arg, leaves, memo))
    else:
        out = e
    memo[id(e)] = out
    return out


def _out_storage(dev, t: Tensor, stat: bool) -> tuple:
    if stat:
        st = Storage(dev, RW.stat_bytes(RW.rows_of(t.rows)))
        return st, st.addr
    R, W = RW.rows_of(t.rows), t.rows[-1]
    st = Storage(dev, max(32, 2 * (R + V.overrun(R)) * W))
    return st, st.addr


def _realize(t: Tensor) -> None:
    node, dev = t._node, D.device()
    if node is None:
        return
    leaves = _leaves(node.expr)
    prod = _prod_of(node.expr)
    if prod is not None:
        leaves = [prod] + [x for x in leaves if x is not prod]
    e = _lower(node.expr, leaves, {})
    view = node.view
    info = {"api": X.count(e), "shape": t.shape}
    if prod is not None:
        st, key = _matmul(dev, t, prod, e, leaves[1:], view, info)
    elif view[-1] % 32 and view[-1] != 1:
        st, key = _planned_rows(dev, t, e, leaves, view, info)
    elif t._stat:
        st, key = _statistic(dev, t, e, leaves, view, info)
    else:
        st, key = _wide(dev, t, e, leaves, view, info), FLAT
    st.wrote(key)
    st.dtype = np.dtype(np.float16)
    t._buf, t._node = Buf(st, np.float16), None


def _bind(dev, leaves, view, stat_space=False) -> tuple:
    binds, ops = {}, []
    for i, x in enumerate(leaves):
        at, o = _addr(dev, x.t, _kind(x.t, view), stat_space)
        binds[f"in{i}"] = at
        ops.append(o)
    return binds, ops


def _wide(dev, t, e, leaves, view, info) -> Storage:
    R, W = RW.rows_of(view), view[-1]
    kinds = [_kind(x.t, view) for x in leaves]
    if W % 32:
        if any(k != "full" for k in kinds) or any(x.t._stat for x in leaves):
            raise Unsupported(
                "API.REF.007",
                f"rows of {W} (not whole 32-column blocks) with a broadcast operand",
            )
        st, out = _out_storage(dev, t, False)
        binds = {
            f"in{i}": FM.f16(dev, x.t.st, x.t.dtype, x.t.size)
            for i, x in enumerate(leaves)
        }
        FM.generated.flat(
            dev,
            e,
            tuple("f16" for _ in leaves),
            t.size,
            dict(binds, out=out),
            info=info,
        )
        return st
    binds, ops = _bind(dev, leaves, view)
    st, out = _out_storage(dev, t, False)
    try:
        RW.run(dev, e, ops, binds, R, W, Operand("full", pitch=W), out, info)
    except (RW.RowsError, ValueError) as err:
        st.free()
        raise Unsupported("API.REF.007", str(err)) from None
    return st


def _statistic(dev, t, e, leaves, view, info) -> tuple:
    R = RW.rows_of(view)
    st, out = _out_storage(dev, t, True)
    try:
        if view[-1] == 1:
            # Statistics of statistics: R values a statistic, 16 a word.
            binds, ops = _bind(dev, leaves, view, stat_space=True)
            RW.run(dev, e, ops, binds, R, 32, Operand("row"), out, info)
        else:
            if view[-1] % 32:
                raise Unsupported(
                    "API.REF.007",
                    f"a reduction of rows of {view[-1]}: whole 32-column blocks",
                )
            binds, ops = _bind(dev, leaves, view)
            RW.run(dev, e, ops, binds, R, view[-1], Operand("row"), out, info)
    except (RW.RowsError, ValueError) as err:
        st.free()
        raise Unsupported("API.REF.007", str(err)) from None
    return st, FLAT


def _access(t: Tensor, slot: int, ranges: tuple, broadcast: bool = True) -> Access:
    """A tensor's dense read, broadcast over the planner's output ranges."""
    shape = (1,) * (len(ranges) - len(t.rows)) + t.rows
    stride, coef = 1, []
    for r, extent in reversed(tuple(zip(ranges, shape, strict=True))):
        if extent != 1 or not broadcast:
            coef.append((r, stride))
        stride *= extent
    dtype = dtypes.float if t.dtype == np.float32 else dtypes.half
    return Access(slot, dtype, tuple(reversed(coef)), 0)


def _planned_rows(dev, t, e, leaves, view, info) -> tuple:
    """Logical row axes through the device planner, including partial columns.

    Each reduction owns its column range; a flat 128-value trip is not a
    logical row. Nested reductions retain their distinct read coordinates.
    """
    ranges = tuple(UOp.range(n, i, AxisType.LOOP) for i, n in enumerate(view))
    accesses, reductions, memo = [], {}, {}
    next_axis = len(ranges)

    def walk(x, axes):
        nonlocal next_axis
        key = (id(x), axes)
        if key in memo:
            return memo[key]
        if isinstance(x, X.Load):
            a = _access(leaves[x.i].t, x.i + 1, axes)
            result = X.Load(len(accesses))
            accesses.append(a)
        elif isinstance(x, X.Reduce):
            col = UOp.range(view[-1], next_axis, AxisType.REDUCE)
            next_axis += 1
            result = X.Reduce(x.op, walk(x.arg, (*axes[:-1], col)))
            reductions[id(result)] = (col,)
        elif isinstance(x, X.Op):
            result = X.Op(x.name, tuple(walk(a, axes) for a in x.args))
        else:
            result = x
        memo[key] = result
        return result

    expr = walk(e, ranges)
    kept = ranges[:-1] if t._stat else ranges
    plan = Plan(
        "vector",
        None,
        "api_rows",
        0,
        dtypes.half,
        view[:-1] if t._stat else view,
        kept,
        expr=expr,
        accesses=accesses,
        reduce_ranges=tuple(r for rs in reductions.values() for r in rs),
        reductions=reductions,
    )
    st = Storage(dev, 2 * t.size)
    bufs = {0: st, **{i + 1: x.t.st for i, x in enumerate(leaves)}}
    try:
        for run in R.execute(dev, plan, bufs):
            run.info.update(info)
    except (Refused, RW.RowsError, FM.FormError, ValueError) as err:
        st.free()
        raise Unsupported("API.REF.007", str(err)) from None
    return st, next(iter(st.valid))


def _matmul(dev, t, prod: Prod, e, extras, view, info) -> tuple:
    """Use the device's product planner, including padding and output forms."""
    a, w = prod.a, prod.b
    ranges = tuple(UOp.range(n, i, AxisType.LOOP) for i, n in enumerate(view))
    batch, (m, n) = ranges[:-2], ranges[-2:]
    k = UOp.range(a.shape[-1], len(ranges), AxisType.REDUCE)
    aa = _access(a, 1, (*batch, m, k), broadcast=False)
    bb = _access(w, 2, (*(batch if w.ndim > 2 else ()), n, k), broadcast=False)
    accesses, bufs = [], {1: a.st, 2: w.st}
    for i, x in enumerate(extras, start=3):
        kind = _kind(x.t, view)
        if x.t._stat and kind in ("full", "col"):
            raise Unsupported("API.REF.003", "a statistic is a value per row")
        accesses.append(_access(x.t, i, ranges))
        bufs[i] = x.t.st
    plan = Plan(
        "matmul",
        None,
        "api_product",
        0,
        dtypes.half,
        t.shape,
        ranges,
        expr=e,
        accesses=accesses,
        a=aa,
        b=bb,
        mnk=(m, n, (k,)),
        batch=batch,
    )
    st = Storage(dev, 2 * t.size)
    bufs[0] = st
    try:
        runs = R.execute(dev, plan, bufs)
        for run in runs:
            run.info.update(info)
    except (Refused, RW.RowsError, FM.FormError, ValueError) as err:
        st.free()
        raise Unsupported("API.REF.007", str(err)) from None
    return st, next(iter(st.valid))


__all__ = [
    "Buf",
    "Node",
    "Prod",
    "Ref",
    "Tensor",
    "Unsupported",
    "linear",
    "matmul",
    "set_fusion",
    "tensor",
]
