"""Index-only values evaluated while compiling: an ast subtree that reads no
buffer (`Tensor.arange`, a causal mask, an iota cast to a float) is a
constant of the kernel's shape, made here in numpy with tinygrad's integer
semantics and uploaded as a table, the way a compiler folds any constant.
Nothing here reads a buffer: data is never computed on the host."""

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops

from kohakutpu.application.ktpugrad import uops as U


def np_dtype(dt):
    """The numpy type of a tinygrad scalar dtype."""
    if dt == dtypes.bool:
        return np.bool_
    if dtypes.is_float(dt):
        return {2: np.float16, 4: np.float32, 8: np.float64}[dt.itemsize]
    if dtypes.is_unsigned(dt):
        return {1: np.uint8, 2: np.uint16, 4: np.uint32, 8: np.uint64}[dt.itemsize]
    return {1: np.int8, 2: np.int16, 4: np.int32, 8: np.int64}[dt.itemsize]


def evaluate(u, ranges: tuple, memo=None):
    """`u` over the loop nest `ranges` (an array broadcastable to their
    extents, one axis each in that order). Raises ValueError on a read."""
    memo = {} if memo is None else memo
    if u in memo:
        return memo[u]
    if u.op is Ops.RANGE:
        if u not in ranges:
            raise ValueError("a range outside the loop nest")
        shape = [1] * len(ranges)
        shape[ranges.index(u)] = U.extent(u)
        out = np.arange(U.extent(u), dtype=np.int64).reshape(shape)
    elif u.op is Ops.CONST:
        out = np.asarray(u.arg)
    elif u.op in (Ops.INDEX, Ops.PARAM, Ops.LOAD):
        raise ValueError("a buffer read is data, not a constant")
    else:
        a = [evaluate(s, ranges, memo) for s in u.src]
        out = _alu(u, a)
    memo[u] = out
    return out


def _alu(u, a):
    op = u.op
    with np.errstate(all="ignore"):
        if op is Ops.CAST:
            return a[0].astype(np_dtype(u.dtype))
        if op is Ops.ADD:
            return a[0] + a[1]
        if op is Ops.SUB:
            return a[0] - a[1]
        if op is Ops.MUL:
            return a[0] * a[1]
        if op is Ops.FDIV:
            return a[0] / a[1]
        if op in (Ops.CDIV, Ops.CMOD):
            # C semantics: the quotient truncates toward zero.
            q = np.abs(a[0]) // np.abs(a[1])
            q = np.where((a[0] < 0) ^ (a[1] < 0), -q, q)
            return q if op is Ops.CDIV else a[0] - a[1] * q
        if op is Ops.FLOORDIV:
            return a[0] // a[1]
        if op is Ops.FLOORMOD:
            return a[0] % a[1]
        if op is Ops.MAX:
            return np.maximum(a[0], a[1])
        if op is Ops.NEG:
            return -a[0]
        if op is Ops.CMPLT:
            return a[0] < a[1]
        if op is Ops.CMPNE:
            return a[0] != a[1]
        if op is Ops.CMPEQ:
            return a[0] == a[1]
        if op is Ops.WHERE:
            return np.where(a[0], a[1], a[2])
        if op is Ops.AND:
            return a[0] & a[1]
        if op is Ops.OR:
            return a[0] | a[1]
        if op is Ops.XOR:
            return a[0] ^ a[1]
        if op is Ops.SHL:
            return a[0] << a[1]
        if op is Ops.SHR:
            return a[0] >> a[1]
        if op is Ops.RECIPROCAL:
            return 1.0 / a[0]
        if op is Ops.SQRT:
            return np.sqrt(a[0])
        if op is Ops.EXP2:
            return np.exp2(a[0])
        if op is Ops.LOG2:
            return np.log2(a[0])
        if op is Ops.SIN:
            return np.sin(a[0])
        if op is Ops.TRUNC:
            return np.trunc(a[0])
        if op is Ops.MULACC:
            return a[0] * a[1] + a[2]
        if op is Ops.POW:
            return np.power(a[0], a[1])
    raise ValueError(f"no constant folding of {op.name}")


def index_only(u, memo=None) -> bool:
    """Whether `u` reads no buffer (only ranges and constants)."""
    memo = {} if memo is None else memo
    if u in memo:
        return memo[u]
    if u.op in (Ops.INDEX, Ops.PARAM, Ops.LOAD, Ops.REDUCE):
        out = False
    elif u.op in (Ops.RANGE, Ops.CONST):
        out = True
    else:
        out = all(index_only(s, memo) for s in u.src)
    memo[u] = out
    return out


__all__ = ["evaluate", "index_only", "np_dtype"]
