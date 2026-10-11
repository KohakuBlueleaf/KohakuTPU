"""L3 -> L2: an `fn NAME.l3` body at a problem size to an `fn NAME.l2` text.

The problem size is the hand L2 body's: each L3 parameter's symbolic shape
against the same-named L2 parameter. The form is chosen by the body:
gemms with a row softmax (attention), other gemms (a gemm chain), no gemm
with [R, C] parameters (rows), else elementwise.
"""

from kohakutpu.language.l2 import reader as l2_reader
from kohakutpu.language.l3 import nodes as N
from kohakutpu.language.l3 import reader as l3_reader
from kohakutpu.language.lower.l3 import attention, gemm, vector
from kohakutpu.language.lower.l3.common import PlanError, dims_of, lets


def problem(module, name: str) -> dict:
    """Parameter -> concrete shape, from the hand `name.l2` signature."""
    body = l2_reader.body(module, name)
    return {p: t.shape for p, t in body.params}


def _all_lets(body) -> list:
    out = []
    for s in body:
        if isinstance(s, N.Let):
            out.append(s)
        elif isinstance(s, (N.Map, N.Scan)):
            out += _all_lets(s.body)
    return out


def plan_l2(module, name: str, options=None) -> str:
    """The L2 module text (target and `fn name.l2`) planned from `name.l3`.
    `options`: `softmax` = `exact` (two passes) or `online`."""
    options = options or {}
    fn = l3_reader.module(module).fns[name]
    shapes = problem(module, name)
    shapes = {p: s for p, s in shapes.items() if p in dict(fn.params)}
    # A parameter read through `transpose` is laid out transposed.
    for s in _all_lets(fn.body):
        if s.op == "transpose" and s.args[0].name in shapes:
            shapes[s.args[0].name] = tuple(reversed(shapes[s.args[0].name]))
    dims = dims_of(fn, shapes)
    if any(isinstance(s, N.Map) for s in fn.body):
        text = attention.heads(fn, dims)
        return f"target {module.target or 'ktpu.v9'}\n\n{text}"
    ops = {s.op for s in lets(fn)}
    if "mmt" in ops and "reduce.max" in ops:
        text = attention.plan(fn, dims, options.get("softmax", "exact"))
    elif "mmt" in ops:
        text = gemm.plan(fn, dims)
    elif any(len(t.shape) == 2 for _, t in fn.params):
        text = vector.rows(fn, dims)
    else:
        text = vector.stream(fn, dims)
    return f"target {module.target or 'ktpu.v9'}\n\n{text}"


__all__ = ["PlanError", "plan_l2", "problem"]
