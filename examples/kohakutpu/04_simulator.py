"""Simulating a kernel: what it computes, what it costs, what crosses the mesh.

READ THIS FILE. It runs, but the point is the source: three levels of detail
behind one call, and when to reach for each.

There is no card here and none is needed. All three levels read the SAME
compiled artifact the driver would dispatch, so they cannot disagree about what
ran -- only about how much they keep.
"""

import numpy as np

from kohakutpu import kernels as K
from kohakutpu import ops as O
from kohakutpu import sim


def numbers() -> None:
    """LEVEL 1 -- what it computes. Grade it against float64, never eyeball it."""
    rng = np.random.default_rng(0)
    x = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
    w = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)

    got = sim.observe(K.linear_silu, x, w)

    ref = x.astype(np.float64) @ w.astype(np.float64).T
    want = ref / (1.0 + np.exp(-ref))
    err = np.abs(got.result.astype(np.float64) - want).max() / np.abs(want).max()
    # ~1.5e-2 is right for this machine: L1 holds MXFP7, not fp16. A tolerance
    # tighter than the format would fail on correct arithmetic.
    print(f"numbers  rel={err:.2e}  saturated={got.saturated}")


def cycles() -> None:
    """LEVEL 2 -- what it costs. Compiles but does not run, so sweeping is cheap.

    The answer is a BRACKET. The low end charges no fill, the high end charges
    every one, and the machine is between: L1 A is double-buffered, so most of a
    fill hides behind the GEMM ahead of it.
    """
    rng = np.random.default_rng(0)
    x = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
    w = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)

    for gn in (4, 8, 16):
        got = sim.cycles(K.linear_silu, x, w, gn=gn)
        # `share` keeps an optimisation honest: 20% off a part worth 18% of the
        # program is 3.6% of the program.
        print(f"cycles   gn={gn:<3} {got.cycles:>6,} cy  VC {got.share('VC'):.0%}")


def mesh() -> None:
    """LEVEL 3 -- what crosses the machine. The level that sees a MAG round trip.

    `stages` is the count that matters: every top-level stage writes its result
    to memory and the next one reads it back.
    """
    rng = np.random.default_rng(0)
    # 128 wide, not 256: `VRED` folds exactly the lanes VL names and VLMAX is
    # 128, so a wider row wants `K.softmax_wide`, which folds hierarchically.
    x = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)

    got = sim.observe(O.softmax, x)
    print(
        f"mesh     stages={got.stages} temps={got.temps} "
        f"dispatches={got.counters['dispatches']} flits={got.counters['flits']:,}"
    )
    print(f"         cores retired {got.cores}")


def main() -> None:
    numbers()
    cycles()
    mesh()
    # Everything above, for one kernel, in one run.
    rng = np.random.default_rng(0)
    x = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
    w = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
    print()
    print(sim.observe(K.linear_silu, x, w).report())


if __name__ == "__main__":
    main()
