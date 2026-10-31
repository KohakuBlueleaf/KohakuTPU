"""The traced graph as plain data: what the Rust backend (`ktpu-graph`)
reads in place of the L2 text (docs/spec/stack/dsl/graph.md, DSL.GRAPH).

Every piece is JSON: an index is an int or ``{"const", "terms"}``; a view
``{"buffer", "axes"}``; an operand, a unit, a keyword attribute and a
statement are objects tagged by their kind. `module` assembles one trace.
"""

import math as _math
import numbers

from kohakutpu.language.dsl import expr as E

FORMAT = "ktpu.graph"
VERSION = 1


# ------------------------------------------------------------------ indices
def index(x):
    """An int, or ``{"const": C, "terms": [[ATOM, COEF], ...]}`` in the
    `Sym`'s term order."""
    if isinstance(x, E.Sym):
        return {"const": x.const, "terms": [[atom(a), c] for a, c in x.terms.items()]}
    return int(x)


def atom(a) -> dict:
    """``{"var": NAME}`` or ``{"floor" | "mod" | "mul": [A, B]}``."""
    if isinstance(a, E.Var):
        return {"var": a.name}
    tag = {"/": "floor", "%": "mod", "*": "mul"}[a.op]
    return {tag: [index(a.a), index(a.b)]}


def cond(c: E.Cond) -> dict:
    return {"op": c.op, "a": index(c.a), "b": index(c.b)}


# -------------------------------------------------------- buffers and views
def layout_value(k, v):
    if isinstance(v, bool):
        return {"bool": v}
    if isinstance(v, int):
        return {"int": v}
    if isinstance(v, str):
        return {"str": v}
    return {"ints": [int(x) for x in v]}


def spec(s) -> dict:
    return {
        "dtype": s.dtype,
        "shape": list(s.shape),
        "layout": [[k, layout_value(k, v)] for k, v in s.layout],
        "space": s.space,
    }


def axis(a):
    """``"all"``, ``"new"``, ``{"run": [LO, SIZE]}``, ``{"idx": E}``."""
    if a[0] in ("all", "new"):
        return a[0]
    if a[0] == "run":
        return {"run": [index(a[1]), a[2]]}
    return {"idx": index(a[1])}


def view(v) -> dict:
    return {"buffer": v.param.name, "axes": [axis(a) for a in v.axes]}


def unit(u) -> dict:
    """``{"kind", "index"}``, index None for ``KIND[any]``."""
    return {"kind": u.kind, "index": None if u.index is None else index(u.index)}


def vtype(v) -> dict:
    return {
        "dtype": v.dtype,
        "shape": list(v.shape),
        "space": v.space,
        "mode": v.mode,
    }


# ----------------------------------------------------------------- operands
def number(x: float):
    """A float, or its name when JSON has no number for it."""
    if _math.isfinite(x):
        return x
    return "nan" if _math.isnan(x) else ("inf" if x > 0 else "-inf")


def value(name: str) -> dict:
    return {"value": name}


def bcast(name: str) -> dict:
    """A statistic broadcast across a row (``NAME[:, *]``)."""
    return {"bcast": name}


def literal(x: float) -> dict:
    return {"float": number(x)}


def value_view(vv) -> dict:
    return {"vview": {"value": vv.value.name, "axes": [axis(a) for a in vv.axes]}}


def buffer_view(v) -> dict:
    return {"view": view(v)}


def unit_ref(u) -> dict:
    return {"unit": unit(u)}


# -------------------------------------------------------- keyword attributes
def attr(k: str, v) -> dict:
    """A statement keyword: ``{"int"}``, ``{"str"}``, ``{"walk": [[N, S]]}``,
    ``{"perm": [..]}``, ``{"view": VIEW}``, ``{"index": E}``."""
    if k in ("walk", "dwalk"):
        return {"walk": [[int(n), int(s)] for n, s in v]}
    if k == "perm":
        return {"perm": [int(p) for p in v]}
    if k in ("index", "rhs"):
        return {"view": view(v)}
    if isinstance(v, E.Sym):
        return {"index": index(v)}
    if isinstance(v, numbers.Integral) and not isinstance(v, bool):
        return {"int": int(v)}
    return {"str": str(v)}


def attrs(kw: dict | None) -> list:
    """The keywords that print (None omitted), in order."""
    return [[k, attr(k, v)] for k, v in (kw or {}).items() if v is not None]


# --------------------------------------------------------------- the module
def block(stmts: list) -> list:
    """A recorded block: each statement's node with its target, source line
    and body."""
    out = []
    for s in stmts:
        # A statement emitted with no node (a patched recorder) keeps its text.
        n = dict(s.node) or {"kind": "unrecorded", "op": s.op, "args": list(s.args)}
        if n["kind"] == "value":
            n["target"] = s.target
        if s.body is not None:
            n["body"] = block(s.body)
        n["where"] = s.where
        out.append(n)
    return out


def plain(v):
    """`v` as JSON values (anything else by its repr)."""
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, float):
        return number(v)
    if isinstance(v, (bool, int, str)) or v is None:
        return v
    return repr(v)


def module(trace, target: str, roles: dict, consts: dict) -> dict:
    """One trace: kernel signature, scratch, constants, machine and body."""
    m = trace.machine
    return {
        "format": FORMAT,
        "version": VERSION,
        "name": trace.name,
        "target": target,
        "machine": {"mg": m.mg, "vc": m.vc, "nodes": m.nodes},
        "consts": plain(consts),
        "ports": [
            {"name": n, "role": roles[n], "spec": spec(s)} for n, s in trace.params
        ],
        "scratch": [{"name": n, "spec": spec(s)} for n, s in trace.scratch.items()],
        "body": block(trace.root),
    }


__all__ = [
    "FORMAT",
    "VERSION",
    "attrs",
    "bcast",
    "block",
    "buffer_view",
    "cond",
    "index",
    "literal",
    "module",
    "spec",
    "unit",
    "unit_ref",
    "value",
    "value_view",
    "view",
    "vtype",
]
