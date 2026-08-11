"""Tile choice and the grid over it.

The tile is a property of the problem, the grid divides it across clusters.
Conflating them: .plan/measurements/dispatch-n-only.md.
"""

from dataclasses import dataclass

from ktpu.ir.sched import Band, Grid, ScheduleError, Tile
from ktpu.target import Target

# Entries one CHUNK may hold: `boff + h*nk + kb` is 8 bits
# (mx_cluster_mgr.v), so entry 256 wraps onto entry 0 and reads another block.
L1_OFF_SPAN = 256


@dataclass(frozen=True)
class TileChoice:
    """A tile in both units: gm/gn count sub-tiles, `tile` counts elements."""

    gm: int
    gn: int
    nk: int
    tile: Tile
    intensity: float
    useful: float

    @property
    def score(self) -> float:
        return self.intensity * self.useful

    def __str__(self) -> str:
        return (
            f"gm={self.gm} gn={self.gn} nk={self.nk} -> {self.tile} "
            f"(intensity {self.intensity:.1f}, useful {self.useful:.2f})"
        )


def _ceil_to(v: int, q: int) -> int:
    return -(-v // q) * q


def choose_tile(m: int, k: int, n: int, t: Target) -> TileChoice:
    """Pick the output tile for an `m x k x n` GEMM on target `t`.

    Searches power-of-two `(gm, gn)` sub-tile counts with `gm*gn <= t.tiles`,
    taking `nk = min(bank // gm, bank // gn, k // kblock)` K blocks per fill,
    where a bank is `min(l1 // banks, L1_OFF_SPAN)`. Candidates are ranked by arithmetic intensity `2*gm*gn/(gm+gn)`
    multiplied by `useful`, the fraction of the padded problem that is the real
    problem; ties go to larger `nk`, then to the squarer tile.

    `m, k, n` must be the WHOLE problem, not a per-cluster slice -- the tile is
    a property of the problem and the grid divides it afterwards.

    Returns the winning TileChoice. Raises ScheduleError if no `(gm, gn)`
    admits `nk >= 1`.
    """
    k_blocks = max(1, k // t.kblock)
    best: tuple[tuple, TileChoice] | None = None

    sizes = [1 << e for e in range(12) if (1 << e) <= t.tiles]
    for gm in sizes:
        for gn in sizes:
            if gm * gn > t.tiles:
                continue
            # Dividing B by `l1_b` alone let gn*nk reach 512, and 64x288x256
            # came back off by 8.2e+02 where 64x256x256 was right.
            bank_a = min(t.l1_a // t.l1_a_banks, L1_OFF_SPAN)
            bank_b = min(t.l1_b // t.l1_a_banks, L1_OFF_SPAN)
            nk = min(bank_a // gm, bank_b // gn, k_blocks)
            if nk < 1:
                continue

            bm, bn = t.block(gm, gn)
            bk = nk * t.kblock
            useful = (m * k * n) / (_ceil_to(m, bm) * _ceil_to(k, bk) * _ceil_to(n, bn))
            intensity = 2 * gm * gn / (gm + gn)

            cand = TileChoice(gm, gn, nk, Tile(m=bm, n=bn, k=bk), intensity, useful)
            key = (round(cand.score * 4096), nk, -abs(gm - gn))
            if best is None or key > best[0]:
                best = (key, cand)

    if best is None:
        raise ScheduleError(
            f"no tile fits {t.name}: l1_a={t.l1_a} l1_b={t.l1_b} tiles={t.tiles}"
        )
    return best[1]


def grid_for(m: int, n: int, choice: TileChoice, sk: int = 1) -> Grid:
    """Build the dispatch grid covering an `m x n` output with `choice`.

    Returns a Grid of `ceil(m/tile.m) x ceil(n/tile.n) x sk`. Each `(mo, no)`
    is one unit of work assignable to any cluster; `sk > 1` splits the reduction
    so instances sharing `(mo, no)` produce partials needing a reduction band.

    Runs `Grid.covers`, so it raises ScheduleError on a grid that would leave
    output uncovered or give an instance no work.
    """
    g = Grid(
        m=_ceil_to(m, choice.tile.m) // choice.tile.m,
        n=_ceil_to(n, choice.tile.n) // choice.tile.n,
        sk=sk,
    )
    g.covers(m, n, choice.tile)
    return g


def epilogue_grid(producer: Band, t: Target) -> tuple[Grid, Tile]:
    """Grid and tile for a band consuming `producer`'s tiles while resident.

    Starts at one instance per producer tile and splits each tile along ROWS
    while that leaves fewer instances than `t.vector_cores`, so a matmul grid
    smaller than the vector mesh still fills it. Rows are split rather than
    columns because a drained tile is laid out row-group major, so a row slice
    is a contiguous span of C and a column slice is not.

    Splitting stops when a slice would fall below `t.lanes` rows, below `t.vlmax`
    elements, or when the row count stops halving evenly -- a vector instance
    smaller than one pass of the machine costs more in setup than it saves.

    Returns `(grid, tile)` satisfying `sched.correspondence(producer, ...)`. The
    result deliberately says nothing about WHICH vector core takes which
    instance: that is codegen's to decide, and it is not always one to one.
    """
    sm = 1
    while producer.grid.size * sm * 2 <= max(1, t.vector_cores):
        rows = producer.tile.m // (sm * 2)
        if producer.tile.m % (sm * 2) or rows % t.lanes:
            break
        if rows * producer.tile.n < t.vlmax:
            break
        sm *= 2
    grid = Grid(m=producer.grid.m * sm, n=producer.grid.n)
    return grid, Tile(m=producer.tile.m // sm, n=producer.tile.n)


def occupancy(grid: Grid, t: Target) -> tuple[int, int]:
    """Return `(fed, available)` -- clusters this grid can occupy, and total.

    `fed` is `min(grid.size, t.clusters)`. Reported rather than asserted,
    because an under-fed grid is legal: a problem may be smaller than the mesh.
    """
    return min(grid.size, t.clusters), t.clusters
