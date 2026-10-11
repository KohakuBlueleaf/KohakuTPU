"""L2 nodes: a body of placed instances, ordered loops and tile ops.

Index expressions stay syntax terms (`Int`, `Name`, `BinOp`, `Offset`, `Neg`)
and are evaluated under the enclosing loop variables (`verify.value`).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Type:
    """`dtype[shape] @space(layout)`; space None is the unit's registers."""

    dtype: str
    shape: tuple
    space: str | None = None
    layout: tuple = ()


@dataclass(frozen=True)
class Axis:
    """One axis of a view: the whole axis (`:`), `lo : +size`, one element
    (`lo`, the axis dropped) or a broadcast axis (`*`)."""

    lo: object = None
    size: int | None = None
    new: bool = False


@dataclass(frozen=True)
class View:
    name: str
    axes: tuple = ()


@dataclass(frozen=True)
class Const:
    value: float


@dataclass(frozen=True)
class Buffer:
    name: str
    type: Type


@dataclass(frozen=True)
class Par:
    """Instances `var` in lo..hi, instance `var` on `unit[at]`."""

    var: str
    lo: int
    hi: int
    unit: str
    at: object
    body: tuple


@dataclass(frozen=True)
class For:
    var: str
    lo: int
    hi: int
    pipe: int
    body: tuple


@dataclass(frozen=True)
class Load:
    name: str
    src: View
    type: Type


@dataclass(frozen=True)
class Op:
    """`name = op args attrs : type`; an inline store value has no name."""

    name: str | None
    op: str
    args: tuple
    attrs: tuple = ()
    type: Type | None = None


@dataclass(frozen=True)
class Store:
    dst: View
    value: object


@dataclass
class Body:
    name: str
    params: tuple
    stmts: tuple
    #: id(node) -> the statement it was read from, for diagnostics.
    at: dict = field(default_factory=dict)
    #: The module text and file name the body was read from.
    source: str = ""
    file: str = "<l2>"


__all__ = [
    "Axis",
    "Body",
    "Buffer",
    "Const",
    "For",
    "Load",
    "Op",
    "Par",
    "Store",
    "Type",
    "View",
]
