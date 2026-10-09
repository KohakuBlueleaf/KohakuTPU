"""The cluster tiling a matmul-shaped kernel runs with, chosen per call.

`gm x gn` sub-tiles of 4x4 stay resident in the accumulator while `nk` K-blocks
of 32 stream through the two L1 banks, one bank filling while the other is
swept. A kernel opts in with `@kernel(tiler=MatmulTiler("M", "K", "N"))`; a
caller that names `gm`, `gn` or `nk` keeps its own.

The limits are the cluster's (mx_cluster_cu.v, the generated mesh tops) and the
costs were measured on the Verilated card; docs/projects/kohakutpu/tiling.md.
"""

from dataclasses import dataclass

from kohakuaccel.lang.iface import BATCH
from kohakutpu.isa.cluster import ISA as CU

from kohakutpu import layout as LO

LANES, KBLOCK = LO.LANES, LO.KBLOCK
#: A FILL counts entries in 8 bits; 256 wraps to 0.
FILL_ENTRIES = CU.cfg.max_fill
#: Resident sub-tiles: `TILES` of every generated mesh top.
ACC_TILES = 4096

#: Node cycles, measured on the Verilated card (one MG, 300 MHz node and NoC):
#: a 128-entry FILL is 595 cycles, a 64x64 nk 2 sweep (8,192 issues) 4,108.
ENTRY_CYCLES = 4.25  # one 128-B entry streamed, staging or DRAM: 4 flits
FILL_CYCLES = 50.0  # a FILL's descriptor round trip before its first entry
ISSUE_CYCLES = 0.5  # one 4x4x32 sub-tile issue: one mat2x cycle
SWEEP_CYCLES = 12.0  # a sweep's cascade and accumulator tail
DRAIN_CYCLES = 1.4  # one sub-tile written back after the last sweep
#: The dispatcher's cost per instruction word while FILLs stream: words reach a
#: cluster ~780 apart (MX_SEQ_TRACE, v9 256^3), 325 with the mesh idle.
WORD_CYCLES = 780.0
PACKAGE_CYCLES = 2200.0  # a package's header, bindings and final barrier
#: Fill streams the memory side serves at once. MEASURED 1 on the card models:
#: v9's four clusters, two per MAG port, filled strictly one after another.
MEM_PATHS = 1


@dataclass(frozen=True)
class Tiling:
    gm: int
    gn: int
    nk: int
    cycles: float
    words: int


def divisors(n: int, cap: int) -> list:
    """Every divisor of `n` up to `cap`."""
    return [d for d in range(1, min(n, cap) + 1) if n % d == 0]


def ceil(a: int, b: int) -> int:
    return -(-a // b)


def cost(
    gy: int, gx: int, kb: int, batch: int, units: int, gm: int, gn: int, nk: int
) -> Tiling:
    """Cycles to run a `gy x gx` grid of output groups over `kb` K-blocks.

    Per K step one bank fills (A then B) while the other is swept, so a step
    costs the larger of the two; the first fill and the last sweep are not
    hidden, and the drain adds its per-sub-tile tail after the last sweep.

    ONE dispatcher sends every word serially, instances dealt round-robin: an
    instance begins once its first word is out and its unit is free, and ends
    no sooner than its last word plus the final sweep and drain (v9, 256^3: a
    per-wave charge priced 16 instances at 19.7k; they ran 55k). Every entry
    of every instance crosses the memory side's `MEM_PATHS` streams, so the
    plan ends no sooner than all of them plus one tail.
    """
    steps = ceil(kb, nk)
    fill = 2 * FILL_CYCLES + (gm + gn) * nk * ENTRY_CYCLES
    # The double-pumped array issues K-blocks in pairs: an odd one costs a pair
    # (measured: nk=1 sweeps as long as nk=2, 72,250 vs 39,440 at 256x512x256).
    sweep = gm * gn * (nk + nk % 2) * ISSUE_CYCLES + SWEEP_CYCLES
    words = 3 * steps + 1
    tail = sweep + gm * gn * DRAIN_CYCLES
    run = fill + (steps - 1) * max(fill, sweep) + tail
    instances = batch * ceil(gy, gm) * ceil(gx, gn)
    free = [0.0] * min(instances, units)
    end = 0.0
    for i in range(instances):
        u = i % units
        begin = max(free[u], (i * words + 1) * WORD_CYCLES)
        free[u] = max(begin + run, (i + 1) * words * WORD_CYCLES + tail)
        end = max(end, free[u])
    entries = instances * steps * (gm + gn) * nk
    end = max(end, entries * ENTRY_CYCLES / MEM_PATHS + tail)
    total = PACKAGE_CYCLES + end
    return Tiling(gm, gn, nk, total, instances * words)


def choose(
    m: int, k: int, n: int, units: int, batch: int = 1, acc: int = ACC_TILES
) -> Tiling:
    """The tiling of an `m x k` by `k x n` product that the model prices lowest.

    Every candidate fits the hardware: `gm * gn` of the `acc` resident
    sub-tiles, and one FILL of `gm * nk` or `gn * nk` entries per bank.
    """
    gy, gx, kb = ceil(m, LANES), ceil(n, LANES), ceil(k, KBLOCK)
    best = None
    for gm in divisors(gy, FILL_ENTRIES):
        for gn in divisors(gx, FILL_ENTRIES):
            if gm * gn > acc:
                continue
            for nk in divisors(kb, FILL_ENTRIES):
                if max(gm, gn) * nk > FILL_ENTRIES:
                    continue
                t = cost(gy, gx, kb, batch, max(1, units), gm, gn, nk)
                if best is None or (t.cycles, t.words) < (best.cycles, best.words):
                    best = t
    return best


@dataclass(frozen=True)
class MatmulTiler:
    """A kernel's tiler: which extents are the product's M, K and N."""

    m: str
    k: str
    n: str
    knobs = ("gm", "gn", "nk")

    def __call__(self, machine, extents: dict) -> dict:
        t = choose(
            extents[self.m],
            extents[self.k],
            extents[self.n],
            machine.count("MG"),
            int(extents.get(BATCH, 1) or 1),
            int(getattr(machine, "tiles", ACC_TILES)),
        )
        return {"gm": t.gm, "gn": t.gn, "nk": t.nk}
