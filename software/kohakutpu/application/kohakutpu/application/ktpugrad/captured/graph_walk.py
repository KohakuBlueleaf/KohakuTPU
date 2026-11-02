"""Exact graph reachability at the eager seam, with no retained graph cache."""

from tinygrad import tensor
from tinygrad.device import Buffer
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp, _rebuild_dtype

from kohakutpu.language.config import ENV
from kohakutpu.stack.rs import _rs

_ORIGINAL = tensor._apply_map_to_tensors
ENABLED = False
REWRITE = False


def refresh():
    global ENABLED, REWRITE
    ENABLED = ENV.get("KTPU_NATIVE_GRAPH_WALK", "0") == "1"
    REWRITE = ENV.get("KTPU_NATIVE_GRAPH_REWRITE", "0") == "1"
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


def named(roots):
    """Every buffer the graphs of `roots` name."""
    return [
        b
        for u in _rs.graph_nodes(roots, Ops.BUFFER)
        if isinstance(b := u.buffer, Buffer)
    ]


def buffers():
    """Every buffer some live tensor's graph names."""
    return named(
        [t.uop for ref in list(tensor.all_tensors) if (t := ref()) is not None]
    )


def held():
    return {id(b.base) for b in buffers()}
