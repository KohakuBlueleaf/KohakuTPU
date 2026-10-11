"""Shared pieces of the L3 -> L2 planners: the problem's dimensions, L3
operands as L2 text, value renaming away from memory names."""

from kohakuaccel.ir.l3 import nodes as N
from kohakutpu.ktpu.l2.emit import LowerError

#: The vector cores and clusters a plan places on.
CORES = 2
CLUSTERS = 4


class PlanError(LowerError):
    pass


def dims_of(fn: N.Fn, shapes: dict) -> dict:
    """Symbol -> size, from each parameter's L3 shape and its size in
    `shapes` (parameter -> shape tuple)."""
    dims: dict = {}
    for p, t in fn.params:
        if p not in shapes:
            raise PlanError(f"no size for {p}")
        for d, n in zip(t.shape, shapes[p], strict=True):
            if isinstance(d, str):
                if dims.setdefault(d, n) != n:
                    raise PlanError(f"{d} is {dims[d]} and {n}")
            elif d != n:
                raise PlanError(f"{p}: {t.shape} against {shapes[p]}")
    return dims


def size(shape: tuple, dims: dict) -> tuple:
    return tuple(d if isinstance(d, int) else dims[d] for d in shape)


def ty(dtype: str, shape: tuple, space: str | None = None) -> str:
    dims = ", ".join(str(d) for d in shape)
    out = f"{dtype}[{dims}]" if shape else dtype
    return f"{out} @{space}" if space else out


class Names:
    """L3 value names as L2 names: memory names are taken, so a loaded
    parameter and any colliding value get a `t` suffix."""

    def __init__(self, memory) -> None:
        self.memory = set(memory)
        self.map: dict = {}

    def __call__(self, name: str) -> str:
        if name not in self.map:
            new = name
            while new in self.memory or new in self.map.values():
                new += "t"
            self.map[name] = new
        return self.map[name]

    def operand(self, x) -> str:
        match x:
            case N.Ref(name):
                return self(name)
            case N.Const(v):
                return repr(float(v))
            case N.Index(name, entries):
                parts = []
                for e in entries:
                    if isinstance(e, N.Full):
                        parts.append(":")
                    elif isinstance(e, N.NewAxis):
                        parts.append("*")
                    else:
                        raise PlanError(f"no L2 view for the index {e!r}")
                return f"{self(name)}[{', '.join(parts)}]"
        raise PlanError(f"no L2 operand for {x!r}")


def lets(fn: N.Fn) -> list:
    out = []
    for s in fn.body:
        if isinstance(s, N.Let):
            out.append(s)
        elif not isinstance(s, N.Return):
            raise PlanError(
                f"a planned L3 body is lets and a return, not {type(s).__name__}"
            )
    return out


def returned(fn: N.Fn) -> str:
    ret = fn.body[-1]
    if not isinstance(ret, N.Return) or not isinstance(ret.value, N.Ref):
        raise PlanError("the body returns one value")
    return ret.value.name


__all__ = [
    "CLUSTERS",
    "CORES",
    "Names",
    "PlanError",
    "dims_of",
    "lets",
    "returned",
    "size",
    "ty",
]
