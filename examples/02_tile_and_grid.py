"""Tile and grid a GEMM, and see why K-heavy shapes used to starve.

    python examples/02_tile_and_grid.py

Shapes are M x K x N throughout.
"""

from ktpu.ir import OpKind
from ktpu.ir.sched import Band, Engine, SchedOp, Schedule, ScheduleError, Tile
from ktpu.passes import choose_tile, grid_for, occupancy
from ktpu.target import VU13P_8CU as T

SHAPES = [
    (256, 1024, 256, "K-heavy: what the legacy N-only split starved"),
    (512, 256, 1024, "the recorded 8-cluster reference"),
    (64, 4096, 64, "skinny and deep: one tile at any 2D grid"),
    (300, 1024, 300, "ragged: padding discount picks a smaller tile"),
]

for m, k, n, note in SHAPES:
    c = choose_tile(m, k, n, T)
    g = grid_for(m, n, c)
    fed, have = occupancy(g, T)
    print(f"{m}x{k}x{n:<5} {note}")
    print(f"    tile {c}")
    print(f"    grid {g}  =  {g.size} tiles   clusters fed {fed}/{have}")

print("\n--- the tile does not depend on the cluster count ---")
for clusters in (1, 2, 4, 8):
    t = T.__class__(clusters=clusters)
    c = choose_tile(256, 1024, 256, t)
    print(f"    {clusters} clusters -> gm={c.gm} gn={c.gn} nk={c.nk}")

print("\n--- split-K needs a reduction band, and verify() says so ---")
c = choose_tile(64, 4096, 64, T)
s = Schedule(name="skinny")
s.add(
    Band(
        engine=Engine.MATMUL,
        grid=grid_for(64, 64, c, sk=8),
        tile=c.tile,
        ops=[SchedOp(OpKind.MATMUL)],
        name="mm",
    )
)
try:
    s.verify()
except ScheduleError as e:
    print(f"    {e}")

s.add(
    Band(
        engine=Engine.VECTOR,
        grid=grid_for(64, 64, c),
        tile=Tile(m=c.tile.m * c.tile.n),
        ops=[SchedOp(OpKind.ADD)],
        reduces="mm",
        name="epilogue",
    )
)
s.verify()
print("    with the epilogue added, it verifies:\n")
print(s)
