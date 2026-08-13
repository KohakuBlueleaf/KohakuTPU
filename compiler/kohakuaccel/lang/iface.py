"""A kernel's interface: what it takes, what it produces, and at what extents.

A port is declared as a parameter DEFAULT, `a=In(M, K)`. Binding `a=x` solves
`M` and `K` from `x.shape`, so an `Out(M, N)` has a known size and the compiler
allocates it. This level knows names, roles and extents, not what a port means.
"""

import inspect
from dataclasses import dataclass

from kohakuaccel.lang.dims import Dim, Name, free, resolve


class IfaceError(ValueError):
    """A signature that cannot be read, or arguments that do not fit it."""


class PortSpec:
    """Base for anything usable as a port declaration.

    A project subclasses this so its kernels declare ports in the type its own
    bodies expect.
    """

    port_role = "in"

    def __init__(self, *shape) -> None:
        self.port_shape = tuple(shape)

    def __repr__(self) -> str:
        inner = ", ".join(str(a) for a in self.port_shape)
        return f"{type(self).__name__}({inner})"


class In(PortSpec):
    """An operand the kernel reads. Its extents are solved from the argument."""

    port_role = "in"


class Out(PortSpec):
    """A result the kernel writes, allocated by the compiler and returned."""

    port_role = "out"


def is_port(value) -> bool:
    """Whether `value` declares a port."""
    return hasattr(value, "port_role") and hasattr(value, "port_shape")


#: Batch elements a call covers, 1 when nothing is batched.
BATCH = "__batch__"
#: The leading axes an ellipsis absorbed, as a tuple of numbers.
LEAD = "__lead__"
#: Total elements of a whole-array ellipsis port.
FLAT = "__flat__"


@dataclass(frozen=True)
class Port:
    """One declared operand or result.

    A leading `...` absorbs axes the kernel does not tile. With no trailing axis
    -- `In(...)` -- the port is ONE CONTIGUOUS BLOCK of whatever rank it is
    handed. With trailing axes -- `In(..., L, D)` -- the leading axes are a BATCH
    and the body describes one element of it.
    """

    name: str
    role: str
    shape: tuple
    position: int

    @property
    def is_in(self) -> bool:
        return self.role == "in"

    @property
    def batched(self) -> bool:
        return self.shape[:1] == (Ellipsis,)

    @property
    def trailing(self) -> tuple:
        """The axes the kernel actually tiles."""
        return self.shape[1:] if self.batched else self.shape

    def resolve(self, extents: dict) -> tuple:
        """This port's full shape as numbers, batch axes included."""
        if not self.batched:
            return tuple(resolve(a, extents) for a in self.shape)
        lead = tuple(extents.get(LEAD, ()))
        return lead + tuple(resolve(a, extents) for a in self.trailing)

    def resolve_trailing(self, extents: dict) -> tuple:
        """One batch element's shape, which is what a layout is chosen for."""
        if self.batched and not self.trailing:
            return (resolve(Name(FLAT), extents),)
        return tuple(resolve(a, extents) for a in self.trailing)


@dataclass(frozen=True)
class Signature:
    """A kernel's ports and its block parameters, read once from the function."""

    ports: tuple
    knobs: dict

    @property
    def inputs(self) -> tuple:
        return tuple(p for p in self.ports if p.is_in)

    @property
    def outputs(self) -> tuple:
        return tuple(p for p in self.ports if not p.is_in)

    def port(self, name: str) -> Port:
        for p in self.ports:
            if p.name == name:
                return p
        raise IfaceError(f"no port named {name!r}; this kernel has {self.names}")

    @property
    def names(self) -> list:
        return [p.name for p in self.ports]


def read(fn) -> Signature:
    """A function's ports and knobs, read from its parameter defaults.

    A parameter defaulting to a port declaration is a port; every other
    parameter is a block parameter and part of the trace's cache key.

    Raises :class:`IfaceError` for a parameter with no default, or a function
    that declares no ports or no In port.
    """
    ports: list = []
    knobs: dict = {}

    for i, (name, param) in enumerate(inspect.signature(fn).parameters.items()):
        default = param.default
        if is_port(default):
            ports.append(Port(name, default.port_role, tuple(default.port_shape), i))
        elif default is inspect.Parameter.empty:
            raise IfaceError(
                f"{fn.__name__}: parameter {name!r} has no default, so it is "
                f"neither a port nor a block parameter. Declare it `= In(M, K)`, "
                f"or give it a value so it can be part of the trace key."
            )
        else:
            knobs[name] = default

    if not ports:
        raise IfaceError(
            f"{fn.__name__} declares no ports; a kernel states what it reads and "
            f"writes in its signature, as `a=In(M, K)`"
        )
    if not any(p.is_in for p in ports):
        raise IfaceError(f"{fn.__name__} has no In port, so no extent can be solved")
    return Signature(tuple(ports), knobs)


def bind(signature: Signature, args: tuple, kwargs: dict) -> dict:
    """Match call arguments to input ports, by position then by name.

    Out ports are never passed; the compiler allocates them. Raises
    :class:`IfaceError` for an unexpected, surplus or missing argument.
    """
    inputs = signature.inputs
    extra = set(kwargs) - {p.name for p in inputs}
    if extra:
        outs = {p.name for p in signature.outputs} & extra
        why = (
            f"{sorted(outs)} are results, which the kernel allocates and returns"
            if outs
            else f"this kernel takes {[p.name for p in inputs]}"
        )
        raise IfaceError(f"unexpected argument {sorted(extra)}: {why}")
    if len(args) > len(inputs):
        raise IfaceError(
            f"{len(args)} arguments for {len(inputs)} operands "
            f"{[p.name for p in inputs]}"
        )

    bound = {p.name: v for p, v in zip(inputs, args)}
    bound.update(kwargs)
    missing = [p.name for p in inputs if p.name not in bound]
    if missing:
        raise IfaceError(f"missing operand {missing}")
    return bound


def solve(signature: Signature, bound: dict, given: dict | None = None) -> dict:
    """Extent name -> value, read off the shapes of the bound arguments.

    An axis written as a bare name is solved; one written as an expression must
    arrive in `given`. A leading `...` absorbs the axes in front of the declared
    ones, and every ellipsis port must agree on what it absorbed.

    Raises :class:`IfaceError` when two operands disagree on an extent, when an
    operand has the wrong rank, when one signature mixes whole-array and
    per-element `...`, or when an extent appears only inside an expression.
    """
    extents: dict = dict(given or {})
    source: dict = {k: "given" for k in extents}
    lead: tuple | None = None
    lead_from = ""
    whole = [p for p in signature.ports if p.batched and not p.trailing]
    per = [p for p in signature.ports if p.batched and p.trailing]
    if whole and per:
        raise IfaceError(
            f"{[p.name for p in whole]} use `...` for the whole array and "
            f"{[p.name for p in per]} use it for a batch; one kernel picks one"
        )

    for port in signature.inputs:
        shape = getattr(bound[port.name], "shape", None)
        if shape is None:
            raise IfaceError(
                f"operand {port.name!r} has no shape; a kernel takes arrays, "
                f"got {type(bound[port.name]).__name__}"
            )
        declared = port.trailing
        if port.batched:
            if len(shape) < len(declared):
                raise IfaceError(
                    f"operand {port.name!r} is declared (..., {declared}) and got "
                    f"{shape}, which has no room for the trailing axes"
                )
            cut = len(shape) - len(declared)
            mine = tuple(shape[:cut])
            if lead is not None and mine != lead:
                raise IfaceError(
                    f"`...` absorbs {lead} in {lead_from} and {mine} in "
                    f"{port.name!r}; batched operands must agree"
                )
            lead, lead_from = mine, repr(port.name)
            shape = shape[cut:]
        elif len(shape) != len(declared):
            raise IfaceError(
                f"operand {port.name!r} is declared {declared} "
                f"({len(declared)}-d) and got a {len(shape)}-d argument {shape}"
            )
        for axis, size in zip(declared, shape):
            if isinstance(axis, Name):
                seen = extents.get(axis.name)
                if seen is not None and seen != size:
                    raise IfaceError(
                        f"extent {axis.name} is {seen} from {source[axis.name]} and "
                        f"{size} from {port.name!r}; the operands disagree"
                    )
                extents[axis.name] = int(size)
                source[axis.name] = repr(port.name)
            elif not isinstance(axis, Dim) and int(axis) != int(size):
                raise IfaceError(
                    f"operand {port.name!r} axis is fixed at {axis} and got {size}"
                )

    unsolved: set = set()
    for port in signature.ports:
        for axis in port.trailing:
            unsolved |= free(axis) - set(extents)
    if unsolved:
        raise IfaceError(
            f"extent {sorted(unsolved)} appears only in an expression, so it "
            f"cannot be read off an argument; pass it explicitly"
        )

    lead = lead or ()
    extents[LEAD] = lead
    # A whole-array `...` is one block of however many elements it was handed;
    # a per-element `...` is that many independent instances.
    extents[FLAT] = _product(lead) if whole else 0
    extents[BATCH] = 1 if whole or not lead else _product(lead)
    return extents


def _product(shape) -> int:
    n = 1
    for axis in shape:
        n *= int(axis)
    return n
