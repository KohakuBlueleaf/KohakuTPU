"""Shared pieces of the L3 -> L2 planners: the problem's dimensions, the
target's units, L3 operands as L2 text, value renaming away from memory
names."""

from kohakutpu.language.l3 import nodes as N
from kohakutpu.language.lower.l2.emit import LowerError


class PlanError(LowerError):
    pass


def cores(target) -> int:
    """The vector cores a plan places on."""
    n = target.units.get("VC", 0)
    if not n:
        raise PlanError(f"{target.name} has no vector core")
    return n


def clusters(target) -> int:
    """The matmul clusters a plan places on."""
    n = target.units.get("MG", 0)
    if not n:
        raise PlanError(f"{target.name} has no matmul cluster")
    return n


def all_lets(body) -> list:
    """Every let of `body`, inside maps and scans too."""
    out = []
    for s in body:
        if isinstance(s, N.Let):
            out.append(s)
        elif isinstance(s, (N.Map, N.Scan)):
            out += all_lets(s.body)
    return out


def transposed(fn: N.Fn) -> set:
    """Parameters the body reads through `transpose`: L2 holds them
    transposed."""
    return {
        s.args[0].name
        for s in all_lets(fn.body)
        if s.op == "transpose" and s.args[0].name in dict(fn.params)
    }


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
    "Names",
    "PlanError",
    "all_lets",
    "clusters",
    "cores",
    "dims_of",
    "lets",
    "returned",
    "size",
    "transposed",
    "ty",
]
