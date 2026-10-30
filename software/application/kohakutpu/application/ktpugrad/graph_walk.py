"""Exact graph reachability at the eager seam, with no retained graph cache."""

import os

from tinygrad import tensor
from tinygrad.device import Buffer
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp, _rebuild_dtype

from kohakutpu.compiler import _rs

_ORIGINAL = tensor._apply_map_to_tensors
ENABLED = False
REWRITE = False


def refresh():
    global ENABLED, REWRITE
    ENABLED = os.environ.get("KTPU_NATIVE_GRAPH_WALK", "0") == "1"
    REWRITE = os.environ.get("KTPU_NATIVE_GRAPH_REWRITE", "0") == "1"
    return ENABLED


refresh()


def apply_map(applied_map, name):
    if not ENABLED:
        return _ORIGINAL(applied_map, name)
    live = [t for ref in list(tensor.all_tensors) if (t := ref()) is not None]
    roots = [t.uop for t in live]
    if REWRITE:
        rewritten = _rs.rewrite_roots(
            roots,
            applied_map,
            Ops.FUNCTION,
            Ops.CALL,
            UOp._OPAQUE_CALL_BODIES,
            _rebuild_dtype,
        )
        for value, before, after in zip(live, roots, rewritten, strict=True):
            if before is not after:
                value.uop = after
        return
    affected = _rs.affected_roots(roots, applied_map)
    scope = [live[i] for i in affected]
    sink = UOp.sink(*(roots[i] for i in affected))
    new = sink.substitute(applied_map, name=f"substitute {name}", walk=True)
    for t, before, after in zip(scope, sink.src, new.src):
        if before is not after:
            t.uop = after


def held():
    roots = [t.uop for ref in list(tensor.all_tensors) if (t := ref()) is not None]
    return {
        id(b.base)
        for u in _rs.graph_nodes(roots, Ops.BUFFER)
        if isinstance(b := u.buffer, Buffer)
    }
