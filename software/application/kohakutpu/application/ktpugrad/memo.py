"""Bounded exact-content memoization for pure launch topology decisions.

No storage, allocation, data, or validity transition is memoized. Dynamic forms
and hints are interpreted by the runner on every call, after these decisions.
"""

import dataclasses
import os
from collections import OrderedDict
from functools import wraps

import numpy as np
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad.lower import Access, Plan

KEPT = 512
ENV = "KTPU_PLAN_CACHE"


class Identity:
    """Retain an immutable expression; equality is identity, never its id alone."""

    def __init__(self, value):
        self.value = value

    def __hash__(self):
        return id(self.value)

    def __eq__(self, other):
        return isinstance(other, Identity) and self.value is other.value


def freeze(value):
    """An equality key retaining full content, not a digest of an AST or array."""
    if isinstance(value, (UOp, X.Load, X.Const, X.Op, X.Reduce)):
        return Identity(value)
    if isinstance(value, Access):
        return (
            Access,
            value.slot,
            value.dtype,
            value.coef,
            value.off,
            freeze(value.table),
            value.gate,
        )
    if isinstance(value, Plan):
        return (
            Plan,
            value.kind,
            Identity(value.ast),
            value.name,
            value.out_slot,
            value.out_dtype,
            value.out_shape,
            value.out_ranges,
            Identity(value.expr),
            tuple(freeze(a) for a in value.accesses),
            value.reduce_ranges,
            tuple(value.reductions.items()),
            freeze(value.value),
            freeze(value.a),
            freeze(value.b),
            value.mnk,
            value.batch,
            value.out_strides,
        )
    if isinstance(value, np.ndarray):
        return (np.ndarray, value.dtype.str, value.shape, value.tobytes())
    if dataclasses.is_dataclass(value):
        return (
            type(value),
            tuple(freeze(getattr(value, f.name)) for f in dataclasses.fields(value)),
        )
    if isinstance(value, dict):
        return (dict, tuple((freeze(k), freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(freeze(v) for v in value))
    if isinstance(value, (set, frozenset)):
        return (type(value), frozenset(freeze(v) for v in value))
    return (type(value), value)


def memo(fn):
    cache = OrderedDict()
    counts = {"hits": 0, "misses": 0}

    @wraps(fn)
    def call(*args, **kwargs):
        if os.environ.get(ENV, "1") == "0":
            return fn(*args, **kwargs)
        key = (freeze(args), freeze(kwargs))
        if key in cache:
            cache.move_to_end(key)
            counts["hits"] += 1
            return cache[key]
        counts["misses"] += 1
        result = fn(*args, **kwargs)
        cache[key] = result
        while len(cache) > KEPT:
            cache.popitem(last=False)
        return result

    call.cache = cache
    call.counts = counts
    return call
