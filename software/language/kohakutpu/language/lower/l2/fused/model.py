"""The fused lowering's tasks: cluster tiles and vector-core RUNs, with the
memory regions they read and write."""

from dataclasses import dataclass, field

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2.verify import value


@dataclass
class Tile:
    """One accumulator tile on a cluster: A rows x B rows over K."""

    unit: int
    a: tuple
    b: tuple
    gm: int
    gn: int
    chunks: int
    out: tuple = None
    reads: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    #: The cluster mark after which this tile is drained.
    drained: str | None = None


@dataclass
class VecTask:
    unit: int
    #: `(names, ops, result)` of an epilogue, or None for a library form.
    inst: object
    image: str | None = None
    #: Argument texts; refs `(buffer, offset)` print as addresses.
    args: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    #: Tiles and earlier vector tasks this task waits for.
    tiles: list = field(default_factory=list)
    after: list = field(default_factory=list)
    lag: int = 0
    #: Lane-element operations, for the bound estimate.
    work: int = 0
    done: str | None = None


def region(view: L.View, env: dict, shape: tuple) -> tuple:
    """`(buffer, ((lo, hi) per axis))` of a view under `env`."""
    out = []
    for a, n in zip(view.axes or (), shape, strict=False):
        if a.lo is None and not a.new:
            out.append((0, n))
        elif a.size is None:
            v = value(a.lo, env)
            out.append((v, v + 1))
        else:
            v = value(a.lo, env)
            out.append((v, v + a.size))
    while len(out) < len(shape):
        out.append((0, shape[len(out)]))
    return view.name, tuple(out)


def overlaps(r1, r2) -> bool:
    if r1[0] != r2[0]:
        return False
    return all(
        a0 < b1 and b0 < a1 for (a0, a1), (b0, b1) in zip(r1[1], r2[1], strict=True)
    )


__all__ = ["Tile", "VecTask", "overlaps", "region"]
