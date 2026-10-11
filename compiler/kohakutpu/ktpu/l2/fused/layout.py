"""Memory layouts the fused lowering gives its buffers, and the address of a
tile in each (docs/projects/kohakutpu/ir/ktpu.md §5).

- `mx7a[M, K] @dram(gm=G, nk=2)`: row tiles of 4G rows, each K/64 chunks of
  4G x 64 entries (one byte a value), chunk-major within the tile.
- `mx7b[N, K] @dram(gn=G, nk=2)`: the same over column tiles of 4G.
- `f16[M, N] @dram(tile=GMxGN)`: drained tiles of 4GM x 4GN, row-major, each
  GM x GN sub-tiles of 4 x 4 values (32 bytes).
"""

from dataclasses import replace

from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2.emit import LowerError

#: K values in one chunk of an MXFP7 tile (NK = 2 sweeps of 32).
KCHUNK = 64
NK = 2
SUB = 4


class Layouts:
    """Each buffer's layout, set once by its first use and checked after."""

    def __init__(self, types: dict) -> None:
        self.types = dict(types)
        self.set: dict = {}

    def want(self, name: str, **kv) -> None:
        got = self.set.get(name)
        if got is not None and got != kv:
            raise LowerError(f"{name} is used as {got} and as {kv}")
        self.set[name] = kv

    def typed(self, name: str) -> L.Type:
        t = self.types[name]
        kv = self.set.get(name)
        if not kv:
            return t
        layout = tuple((k, v) for k, v in kv.items())
        return replace(t, layout=layout)

    # -------------------------------------------------------- addresses
    def mx7(self, name: str, row0: int, k0: int, g: int) -> tuple:
        """`(name, byte offset)` of rows row0.. (a whole 4g tile) at K k0."""
        t = self.types[name]
        _, kfull = t.shape
        tile = SUB * g
        if row0 % tile or k0 % KCHUNK:
            raise LowerError(f"{name}[{row0}, {k0}] is not on a {tile} x {KCHUNK} tile")
        side = "gm" if t.dtype == "mx7a" else "gn"
        self.want(name, **{side: g, "nk": NK})
        return name, (row0 // tile) * tile * kfull + (k0 // KCHUNK) * tile * KCHUNK

    def f16_tile(self, name: str, row0: int, col0: int, gm: int, gn: int) -> tuple:
        t = self.types[name]
        _, cols = t.shape
        th, tw = SUB * gm, SUB * gn
        if row0 % th or col0 % tw or cols % tw:
            raise LowerError(f"{name}[{row0}, {col0}] is not on a {th} x {tw} tile")
        self.want(name, tile=(gm, gn))
        n = (row0 // th) * (cols // tw) + col0 // tw
        return name, n * gm * gn * 32


__all__ = ["KCHUNK", "NK", "SUB", "Layouts"]
