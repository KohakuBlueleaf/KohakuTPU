"""L3 -> L2: an `fn NAME.l3` body at a problem size to an `fn NAME.l2` text,
placed on a target's units.

The problem size is the caller's (`shapes`: each L3 parameter's shape), or,
given none, the hand L2 body's signature. The form is chosen by the body: one
map over heads of attention (the online form), gemms with a row softmax
(attention), other gemms (a gemm chain), no gemm with [R, C] parameters
(rows), else elementwise.
"""

from kohakutpu.language.l2 import reader as l2_reader
from kohakutpu.language.l3 import nodes as N
from kohakutpu.language.l3 import reader as l3_reader
from kohakutpu.language.lower.l3 import attention, gemm, vector
from kohakutpu.language.lower.l3.common import PlanError, dims_of, lets, transposed


def problem(module, name: str) -> dict:
    """L3 parameter -> shape, from the hand `name.l2` signature (which holds
    a parameter the L3 body reads through `transpose` transposed)."""
    if (name, "l2") not in module.fns:
        raise PlanError(f"{name}: no hand L2 body to take the size from; pass shapes")
    fn = l3_reader.module(module).fns[name]
    body = l2_reader.body(module, name)
    shapes = {p: t.shape for p, t in body.params if p in dict(fn.params)}
    for p in transposed(fn) & set(shapes):
        shapes[p] = tuple(reversed(shapes[p]))
    return shapes


def plan_l2(module, name: str, target, shapes=None, options=None) -> str:
    """The L2 module text (target and `fn name.l2`) planned from `name.l3` for
    `target` at `shapes` (L3 parameter -> shape; default `problem`).
    `options`: `softmax` = `exact` (two passes)."""
    options = options or {}
    fn = l3_reader.module(module).fns[name]
    shapes = problem(module, name) if shapes is None else shapes
    dims = dims_of(fn, {p: tuple(s) for p, s in shapes.items()})
    head = f"target {module.target or target.name}\n\n"
    if any(isinstance(s, N.Map) for s in fn.body):
        return head + attention.heads(fn, dims, target)
    ops = {s.op for s in lets(fn)}
    if "mmt" in ops and "reduce.max" in ops:
        text = attention.plan(fn, dims, options.get("softmax", "exact"), target)
    elif "mmt" in ops:
        text = gemm.plan(fn, dims, target)
    elif any(len(t.shape) == 2 for _, t in fn.params):
        text = vector.rows(fn, dims, target)
    else:
        text = vector.stream(fn, dims, target)
    return head + text


__all__ = ["PlanError", "plan_l2", "problem"]
