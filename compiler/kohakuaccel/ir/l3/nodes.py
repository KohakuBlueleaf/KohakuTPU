"""L3, the tile program (docs/spec/l3-program.md): what a kernel computes, as
tiles of tensors, in hardware ops a project names.

A module holds programs and functions. A body is a list of statements, each
one of the classes below; values are single-assignment names scoped to the
block that binds them. A dimension is an int or a symbol (a str) bound by a
parameter's shape or a `Tile`.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Type:
    dtype: str
    shape: tuple = ()


# --------------------------------------------------------------- operands
@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Const:
    value: float


@dataclass(frozen=True)
class Full:
    """``:`` -- a whole dimension."""


@dataclass(frozen=True)
class At:
    """An index: an int, or a loop variable (a point, or a tile of its
    domain's size)."""

    index: object


@dataclass(frozen=True)
class Range:
    """``lo : hi`` of one dimension."""

    lo: object
    hi: object


@dataclass(frozen=True)
class NewAxis:
    """``*`` -- a dimension of 1, for broadcasting."""


@dataclass(frozen=True)
class Index:
    """A view of a value or tensor: one index entry a dimension, leading
    first; missing trailing entries are whole."""

    name: str
    entries: tuple


# ----------------------------------------------------------------- domains
@dataclass(frozen=True)
class Span:
    """Points ``lo .. hi`` (hi excluded)."""

    lo: object
    hi: object


@dataclass(frozen=True)
class Tiles:
    """``extent / size`` tiles of `size` points."""

    extent: object
    size: object


# -------------------------------------------------------------- statements
@dataclass(frozen=True)
class Tile:
    """A named constant dimension."""

    name: str
    value: int


@dataclass(frozen=True)
class Output:
    """A tensor the program writes and returns."""

    name: str
    type: Type


@dataclass(frozen=True)
class Let:
    """``name = op args attrs : type``: one hardware op."""

    name: str
    op: str
    args: tuple
    attrs: tuple = ()
    type: Type | None = None


@dataclass(frozen=True)
class Invoke:
    """``name = inline f(args)`` (unfolded into the caller) or ``call`` (kept a
    function boundary)."""

    name: str
    fn: str
    args: tuple
    inline: bool
    type: Type | None = None


@dataclass(frozen=True)
class Carry:
    """A value a following `Scan` carries from one step to the next."""

    name: str
    init: Const
    type: Type


@dataclass(frozen=True)
class Map:
    """Independent iterations over every point of its domains."""

    vars: tuple  # ((name, Span | Tiles), ...)
    body: tuple


@dataclass(frozen=True)
class Scan:
    """Ordered iterations; the carries before it, updated by `Next`."""

    var: str
    domain: object
    body: tuple


@dataclass(frozen=True)
class Next:
    name: str
    value: object


@dataclass(frozen=True)
class Store:
    target: Index
    value: object


@dataclass(frozen=True)
class Return:
    value: object


# ------------------------------------------------------------------- units
@dataclass(frozen=True)
class Fn:
    """A function: `ret` its result type, its body ending in `Return`."""

    name: str
    params: tuple  # ((name, Type), ...)
    ret: Type
    body: tuple


@dataclass(frozen=True)
class Program:
    """A kernel: tensor parameters in, `Output`s written by `Store`."""

    name: str
    params: tuple
    body: tuple


@dataclass
class Module:
    fns: dict = field(default_factory=dict)
    programs: dict = field(default_factory=dict)
    #: id(node) -> (line, col) of the text it was read from.
    positions: dict = field(default_factory=dict, compare=False, repr=False)
