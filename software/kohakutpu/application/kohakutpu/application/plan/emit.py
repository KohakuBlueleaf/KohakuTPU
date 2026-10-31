"""A `kernels.expr` expression as a JSON node table, a node reached twice stored
once (the generators emit a shared node once)."""

import math

from kohakutpu.application.kernels import expr as X


def number(x: float):
    """A float, or its name when JSON has no number for it."""
    if math.isfinite(x):
        return x
    return "nan" if math.isnan(x) else ("inf" if x > 0 else "-inf")


def expr(e, seen: dict | None = None) -> dict:
    """``{"nodes": [...], "root": i}``: ``{"load": i}``, ``{"const": v}``,
    ``{"op": name, "args": [j..]}``, ``{"reduce": op, "arg": j}``; nodes by
    identity, each before its users. `seen` (``id(node) -> index``) is
    filled for the caller."""
    nodes: list = []
    seen = {} if seen is None else seen

    def walk(x) -> int:
        got = seen.get(id(x))
        if got is not None:
            return got
        if isinstance(x, X.Load):
            node = {"load": x.i}
        elif isinstance(x, X.Const):
            node = {"const": number(float(x.value))}
        elif isinstance(x, X.Op):
            node = {"op": x.name, "args": [walk(a) for a in x.args]}
        elif isinstance(x, X.Reduce):
            node = {"reduce": x.op, "arg": walk(x.arg)}
        else:
            raise TypeError(f"not an expression node: {x!r}")
        seen[id(x)] = len(nodes)
        nodes.append(node)
        return seen[id(x)]

    root = walk(e)
    return {"nodes": nodes, "root": root}


__all__ = ["expr", "number"]
