"""The handles a kernel body holds: buffers (`Param`), views of them (`View`),
the units work runs on (`Unit`) and traced values (`Value`).

A `Value` is one L2 name: its element type, shape, space (``A``, ``B``,
``acc``, ``l1``, ``reg``) and mode (``gt4`` or none). Arithmetic on values
records vector-core statements (`ops.math`); a value never answers a
question about its contents.
"""

import numbers
from typing import Any, NoReturn

from kohakutpu.language.dsl import expr as E
from kohakutpu.language.dsl.errors import DSLError, TracerControlFlowError, at_caller
from kohakutpu.language.dsl.types import TensorSpec

#: The op recorders a value's operators call, bound by `ops` when it loads
#: (it imports this module, so this one cannot import it).
_OPS: dict = {}


def bind(**fns) -> None:
    _OPS.update(fns)


# ------------------------------------------------------------------- units
class Unit:
    """`mg[i]`, `vc[i]` (a unit of this node), `mg[any]`, `node[n]`, and
    another node's unit, `node[n].mg[i]` (a cross-node connection's end)."""

    def __init__(self, kind: str | None, index=None, node=None) -> None:
        self.kind, self.index, self.node = kind, index, node

    def __getitem__(self, i) -> "Unit":
        return Unit(self.kind, i, self.node)

    def _of_node(self, kind: str) -> "Unit":
        if self.kind != "node" or self.index is None:
            raise DSLError(f"{kind} of a node: kt.node[N].{kind}[I], at {at_caller()}")
        return Unit(kind, None, self.index)

    @property
    def mg(self) -> "Unit":
        """This node's clusters: ``kt.node[1].mg[0]``."""
        return self._of_node("mg")

    @property
    def vc(self) -> "Unit":
        return self._of_node("vc")

    def text(self) -> str:
        if self.index is None:
            return f"{self.kind}[any]"
        return f"{self.kind}[{E.text(self.index)}]"

    def __repr__(self) -> str:
        return self.text()


mg, vc, node = Unit("mg"), Unit("vc"), Unit("node")
#: `on=kt.any(kt.vc)`-style choice: the place pass decides.
ANY = object()


# ------------------------------------------------------------------- views
def _axis(k, n: int):
    """One index entry: ``("idx", e)``, ``("run", lo, size)``, ``("all",)``,
    ``("new",)``."""
    if k is None:
        return ("new",)
    if isinstance(k, slice):
        if k.step not in (None, 1):
            raise DSLError(f"a strided slice is not a view, at {at_caller()}")
        if k.start is None and k.stop is None:
            return ("all",)
        lo = 0 if k.start is None else k.start
        hi = n if k.stop is None else k.stop
        size = hi - lo
        if E.is_sym(size):
            raise DSLError(
                f"a slice [{E.text(lo)} : {E.text(hi)}] of a symbolic size, at "
                f"{at_caller()}; write the stop as start + a constant"
            )
        if isinstance(lo, int) and (lo < 0 or lo + size > n or size <= 0):
            raise DSLError(f"[{lo} : {hi}] is outside an axis of {n}, at {at_caller()}")
        return ("run", lo, size)
    if isinstance(k, (int, E.Sym)) and not isinstance(k, bool):
        return ("idx", k)
    raise DSLError(
        f"an index is an int, an index expression, a slice or None, got "
        f"{type(k).__name__}, at {at_caller()}"
    )


class View:
    """A region of a buffer: one entry an axis (leading first)."""

    def __init__(self, param: "Param", axes: list) -> None:
        self.param, self.axes = param, axes

    @property
    def shape(self) -> tuple:
        out, d = [], 0
        for a in self.axes:
            if a[0] == "new":
                out.append(1)
                continue
            if a[0] == "run":
                out.append(a[2])
            elif a[0] == "all":
                out.append(self.param.shape[d])
            d += 1
        out += list(self.param.shape[d:])
        return tuple(out)

    def text(self) -> str:
        if all(a[0] == "all" for a in self.axes):
            return self.param.name
        parts = []
        for a in self.axes:
            if a[0] == "new":
                parts.append("*")
            elif a[0] == "all":
                parts.append(":")
            elif a[0] == "idx":
                parts.append(E.text(a[1]))
            else:
                parts.append(f"{E.text(a[1])} : +{a[2]}")
        while parts and parts[-1] == ":":
            parts.pop()
        return f"{self.param.name}[{', '.join(parts)}]" if parts else self.param.name

    def __repr__(self) -> str:
        return f"View({self.text()})"


class Param:
    """A kernel parameter or a scratch buffer: a name and a `TensorSpec`."""

    def __init__(self, name: str, spec: TensorSpec) -> None:
        self.name, self.spec = name, spec

    @property
    def shape(self) -> tuple[int, ...]:
        return self.spec.shape

    @property
    def dtype(self) -> str:
        return self.spec.dtype

    @property
    def layout(self) -> dict:
        return self.spec.layout_dict

    @property
    def ndim(self) -> int:
        return len(self.spec.shape)

    def __getitem__(self, key: Any) -> View:
        keys = key if isinstance(key, tuple) else (key,)
        axes, d = [], 0
        for k in keys:
            if k is Ellipsis:
                fill = self.ndim - sum(
                    1 for x in keys if x is not None and x is not Ellipsis
                )
                axes += [("all",)] * fill
                d += fill
                continue
            if d >= self.ndim and k is not None:
                raise DSLError(
                    f"{len(keys)} indices for {self.name}{list(self.shape)}, at {at_caller()}"
                )
            axes.append(_axis(k, self.shape[d] if k is not None else 1))
            if k is not None:
                d += 1
        return View(self, axes)

    def whole(self) -> View:
        return View(self, [("all",)] * self.ndim)

    def __repr__(self) -> str:
        return f"Param({self.name}: {self.spec.text()})"


def as_view(x) -> View:
    if isinstance(x, View):
        return x
    if isinstance(x, Param):
        return x.whole()
    raise DSLError(f"wanted a buffer view, got {type(x).__name__}, at {at_caller()}")


# ------------------------------------------------------------------ values
class Value:
    """One traced L2 name."""

    __array_ufunc__ = None

    def __init__(
        self, trace, name, dtype, shape, space, mode=None, unit=None, stmt=None
    ):
        self.trace = trace
        self.name: str = name
        self.dtype: str = dtype
        self.shape: tuple[int, ...] = tuple(shape)
        self.space: str = space
        self.mode: str | None = mode
        self.unit = unit
        self.stmt: Any = stmt
        #: an @l1 value's unpacked register value, made on first use
        self.unpacked: Value | None = None
        #: the block the defining statement sits in
        self.block: Any = None
        #: an @l1 value loaded from a ``tile=`` buffer: its bytes are sub-tiles
        self.tiled = False
        #: a one-row view read as a band: its rows are the band's, not data's
        self.row_bcast = False
        #: a statistic of one value every row reads (`unpack` with `lane=`)
        self.lane = False

    # --------------------------------------------------------- describing
    @property
    def stat(self) -> bool:
        return self.space == "reg" and len(self.shape) == 1

    def type_text(self) -> str:
        dims = ", ".join(str(d) for d in self.shape)
        mode = f"({self.mode})" if self.mode else ""
        return f"{self.dtype}[{dims}] @{self.space}{mode}"

    def __repr__(self) -> str:
        return f"<{self.name}: {self.type_text()}>"

    # ---------------------------------------------------------- refusing
    def _reject(self, what: str, advice: str) -> TracerControlFlowError:
        return TracerControlFlowError(
            f"{what} on the traced value {self.name} ({self.type_text()}), at "
            f"{at_caller()}\n  Its contents exist only when the machine runs, so "
            f"this cannot be answered while tracing.\n  {advice}"
        )

    # Typed NoReturn so a checker flags `if v:` on a traced value in the editor.
    def __bool__(self) -> NoReturn:
        raise self._reject(
            "bool()",
            "`if`, `while`, `and`, `or` and `not` ask for it: make the choice a "
            "value (kt.where(m, a, b)) or a skip (with kt.sticky()); a Python "
            "condition on a Python value is free.",
        )

    def __len__(self) -> NoReturn:
        raise self._reject("len()", "the shape is known: value.shape")

    def __iter__(self) -> NoReturn:
        raise self._reject("iteration", "loop with kt.range over its rows")

    def __index__(self) -> NoReturn:
        raise self._reject("use as an index", "an index computed from data is a gather")

    def __int__(self) -> NoReturn:
        raise self._reject(
            "int()", "there is no .item(); keep compile-time numbers in Python"
        )

    def __float__(self) -> NoReturn:
        raise self._reject(
            "float()", "there is no .item(); keep compile-time numbers in Python"
        )

    def __array__(self, dtype=None, copy=None) -> NoReturn:
        raise self._reject("numpy conversion", "pass arrays in as kernel inputs")

    __hash__ = object.__hash__

    # ------------------------------------------------------- arithmetic
    def _op(self, name: str, *others, flip=False) -> "Value":
        args = (*others[::-1], self) if flip else (self, *others)
        return _OPS["math"](name, *args)

    def __add__(self, o: "Operand") -> "Value":
        return self._op("add", o)

    def __radd__(self, o: float) -> "Value":
        return self._op("add", o, flip=True)

    def __sub__(self, o: "Operand") -> "Value":
        return self._op("sub", o)

    def __rsub__(self, o: float) -> "Value":
        return self._op("sub", o, flip=True)

    def __mul__(self, o: "Operand") -> "Value":
        return self._op("mul", o)

    def __rmul__(self, o: float) -> "Value":
        return self._op("mul", o, flip=True)

    def __truediv__(self, o: "Operand") -> "Value":
        if isinstance(o, numbers.Real):
            return _OPS["math"]("mul", self, 1.0 / float(o))
        return _OPS["math"]("mul", self, _OPS["math"]("inv", o))

    def __rtruediv__(self, o: float) -> "Value":
        inv = _OPS["math"]("inv", self)
        if isinstance(o, numbers.Real) and o == 1:
            return inv
        return _OPS["math"]("mul", o, inv)

    def __neg__(self) -> "Value":
        return self._op("neg")

    def __abs__(self) -> "Value":
        return self._op("abs")

    def __lt__(self, o: "Operand") -> "Value":
        return self._op("cmplt", o)

    def __gt__(self, o: "Operand") -> "Value":
        return self._op("cmpgt", o)

    def __le__(self, o: "Operand") -> "Value":
        return _OPS["math"]("sub", 1.0, self._op("cmpgt", o))

    def __ge__(self, o: "Operand") -> "Value":
        return _OPS["math"]("sub", 1.0, self._op("cmplt", o))

    def eq(self, o: "Operand") -> "Value":
        """1.0 where equal (``==`` is identity, so values stay hashable)."""
        return self._op("cmpeq", o)

    def ne(self, o: "Operand") -> "Value":
        return _OPS["math"]("sub", 1.0, self._op("cmpeq", o))

    def max(self) -> "Value":
        """The row maximum, a statistic (`rmax`)."""
        return _OPS["reduce"]("rmax", self)

    def sum(self) -> "Value":
        """The row sum, a statistic (`rsum`)."""
        return _OPS["reduce"]("rsum", self)

    def unpack(self) -> "Value":
        return _OPS["unpack"](self)

    def assign(self, new: "Value") -> "Value":
        """Rebind this name to `new` (made by the statement just recorded): in
        `with kt.sticky()` the skipped path keeps the old contents."""
        if new.stmt is None or new.stmt.target != new.name:
            raise DSLError(
                f"assign takes a value made by one statement, at {at_caller()}"
            )
        if new.shape != self.shape or new.space != self.space:
            raise DSLError(
                f"{self.name} is {self.type_text()}, the new value {new.type_text()}, "
                f"at {at_caller()}"
            )
        new.stmt.target = self.name
        new.name = self.name
        return new

    def __getitem__(self, key: Any) -> "ValueView":
        keys = key if isinstance(key, tuple) else (key,)
        return ValueView(self, [_axis(k, n) for k, n in zip(keys, self.shape)])


class ValueView:
    """Rows of a value: an @A / @B operand's rows for a gemm, a band of an @l1
    value for a banded unpack, column blocks of a row value for a reduction."""

    def __init__(self, value: Value, axes: list) -> None:
        self.value, self.axes = value, axes

    @property
    def shape(self) -> tuple:
        out = []
        for a, n in zip(self.axes, self.value.shape):
            if a[0] == "run":
                out.append(a[2])
            elif a[0] == "all":
                out.append(n)
        out += list(self.value.shape[len(self.axes) :])
        return tuple(out)

    def text(self) -> str:
        parts = []
        for a in self.axes:
            if a[0] == "all":
                parts.append(":")
            elif a[0] == "idx":
                parts.append(E.text(a[1]))
            else:
                lo = a[1]
                if isinstance(lo, int) and not E.is_sym(lo):
                    parts.append(
                        f"{lo}:{lo + a[2]}"
                        if self._cols(a)
                        else f"{E.text(lo)} : +{a[2]}"
                    )
                else:
                    parts.append(f"{E.text(lo)} : +{a[2]}")
        return f"{self.value.name}[{', '.join(parts)}]"

    def _cols(self, a) -> bool:
        """A column-block slice prints `C0:C1` (the reducer reads constants)."""
        return self.axes.index(a) == 1


#: What vector math takes: a traced value, column blocks of one, or a number.
Operand = Value | ValueView | float

__all__ = [
    "ANY",
    "Operand",
    "Param",
    "Unit",
    "Value",
    "ValueView",
    "View",
    "as_view",
    "mg",
    "node",
    "vc",
]
