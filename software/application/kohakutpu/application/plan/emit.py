"""What the host hands the Rust planner (`ktpu-plan`), as plain JSON.

- `uops`: one tinygrad kernel ast (``ktpu.uops``): its UOps in topological
  order, each ``{op, dtype, src, arg}`` (sources by index, the root last).
- `state`: the bound buffers' records (``ktpu.state``): each base storage's
  size, element type, valid and allocated forms, host bytes; each slot's base
  and byte offset.
- `expr`: a `kernels.expr` expression as a node table, a node reached twice
  stored once (the generators emit a shared node once).
- `operand`, `form`: a `kernels.vector.Operand`, a storage form key.

`Names` gives every storage the planner sees a stable name (``buf0``..),
the same object the same name.
"""

import dataclasses
import enum
import hashlib
import math

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.storage import StorageView

UOPS = "ktpu.uops"
STATE = "ktpu.state"
VERSION = 1


# ----------------------------------------------------------------- values
def number(x: float):
    """A float, or its name when JSON has no number for it."""
    if math.isfinite(x):
        return x
    return "nan" if math.isnan(x) else ("inf" if x > 0 else "-inf")


def plain(x):
    """Any ast argument as JSON: numbers, strings, None as they are; an
    enum ``{"enum": "Class.NAME"}``; a dtype ``{"dtype": "half"}``; a
    dataclass ``{"type", "fields"}``; a sequence a list; else ``{"repr"}``."""
    if isinstance(x, enum.Enum):
        # Before int: `Ops` is an IntEnum.
        return {"enum": f"{type(x).__name__}.{x.name}"}
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, float):
        return number(x)
    if type(x).__name__ == "DType" or type(x).__name__.endswith("DType"):
        return {"dtype": dtype(x)}
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        fields = {f.name: plain(getattr(x, f.name)) for f in dataclasses.fields(x)}
        return {"type": type(x).__name__, "fields": fields}
    if isinstance(x, (tuple, list)):
        return [plain(v) for v in x]
    return {"repr": repr(x)}


def dtype(dt) -> str:
    """A tinygrad dtype by its repr without the ``dtypes.`` prefix
    (``half``, ``float.ptr(64)``)."""
    return repr(dt).removeprefix("dtypes.")


# ------------------------------------------------------------------- uops
def uops(ast) -> dict:
    """``ktpu.uops`` of one kernel ast."""
    order = list(ast.toposort())
    at = {u: i for i, u in enumerate(order)}
    nodes = [
        {
            "op": u.op.name,
            "dtype": dtype(u.dtype),
            "src": [at[s] for s in u.src],
            "arg": plain(u.arg),
        }
        for u in order
    ]
    return {"format": UOPS, "version": VERSION, "nodes": nodes, "root": at[ast]}


# ------------------------------------------------------------- expressions
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


def operand(o: Operand) -> dict:
    return dataclasses.asdict(o)


# ------------------------------------------------------------------ forms
def form(key: tuple) -> list:
    """A form key as JSON: ``["flat"]``, ``["f16"]``, ``["rows", n, np]``,
    ``["mx7", side, OPERAND, rows, cols, rp, cp, g, nk, batches]``."""
    return [operand(k) if isinstance(k, Operand) else plain(k) for k in key]


def host_digest(raw: bytes | None):
    if raw is None:
        return None
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class Names:
    """Stable names for storages: ``buf0``, ``buf1``.. by first sight, or a
    name given (``tmp3``, ``table:..``)."""

    def __init__(self) -> None:
        self.of: dict = {}
        self.objs: list = []

    def name(self, st, given: str | None = None) -> str:
        got = self.of.get(id(st))
        if got is None:
            got = (
                given or f"buf{sum(1 for n in self.of.values() if n.startswith('buf'))}"
            )
            self.of[id(st)] = got
            self.objs.append(st)  # kept alive: an id is not reused while named
        return got


def base_of(s):
    if isinstance(s, StorageView):
        return s.base, s.offset
    return s, 0


def storage(st) -> dict:
    """One base storage's record."""
    return {
        "nbytes": st.nbytes,
        "dtype": None if st.dtype is None else np.dtype(st.dtype).name,
        "valid": sorted((form(k) for k in st.valid), key=repr),
        "forms": sorted(
            ([form(k), f.nbytes] for k, f in st.forms.items() if k != ("flat",)),
            key=repr,
        ),
        "host": host_digest(st.host),
    }


def state(bufs: dict, names: Names) -> dict:
    """``ktpu.state`` of `bufs` (param slot -> storage or view): each slot's
    base name and byte offset, each base's record."""
    slots, bases = {}, {}
    for slot in sorted(bufs, key=repr):
        st, off = base_of(bufs[slot])
        n = names.name(st)
        slots[str(slot)] = {"base": n, "offset": off, "nbytes": bufs[slot].nbytes}
        bases[n] = storage(st)
    return {"format": STATE, "version": VERSION, "slots": slots, "buffers": bases}


__all__ = [
    "STATE",
    "UOPS",
    "VERSION",
    "Names",
    "base_of",
    "dtype",
    "expr",
    "form",
    "host_digest",
    "number",
    "operand",
    "plain",
    "state",
    "storage",
    "uops",
]
