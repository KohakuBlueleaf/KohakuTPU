"""RUN THIS. One trace, every shape -- and what compilation does per shape.

A kernel is traced ONCE and compiled per shape. The body records a symbolic
extent, so what is recorded is the same size whatever the numbers turn out to
be; compilation resolves them, and only then does the sweep unroll.

You write nothing for this. `dims("M, K, N")` already bought it.
"""

import numpy as np

from kohakutpu import api, ops, sim

RULE = "=" * 78
rng = np.random.default_rng(0)

#: None is a multiple of the others, and `k` differs too, so the sweep LENGTH
#: changes and not only the grid.
SHAPES = ((8, 64, 4), (16, 64, 8), (8, 96, 12), (32, 32, 16), (12, 128, 4))


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


dev = api.script_device("sim")
before = ops.matmul.traces

head("FIVE SHAPES THROUGH ONE KERNEL")
print(
    f"  {'M x K x N':<16} {'grid':<10} {'inst':>5} {'stmt':>5} "
    f"{'cycles':>15} {'err':>8}"
)
for m, k, n in SHAPES:
    a = np.asarray(rng.normal(0, 0.5, (m, k)), np.float16)
    b = np.asarray(rng.normal(0, 0.5, (n, k)), np.float16)
    got = ops.matmul(dev.tensor(a), dev.tensor(b))
    want = a.astype(np.float64) @ b.astype(np.float64).T
    err = float(np.abs(np.asarray(got.numpy()) - want).max() / np.abs(want).max())
    made = ops.matmul.plan(dev.tensor(a), dev.tensor(b))
    t = sim.cycles(ops.matmul, a, b)
    print(
        f"  {f'{m} x {k} x {n}':<16} {made.grid!s:<10} "
        f"{len(made.instances):>5} {len(made.instances[0].stmts):>5} "
        f"{t.overlapped:>6,}-{t.cycles:<8,} {err:>7.2%}"
    )

head("THE NUMBER THAT MATTERS")
print(f"  traces recorded across all five shapes: {ops.matmul.traces - before}")
print("\n  ONE. The grid, the instance count, the statements per instance and")
print("  the cycle cost all moved; the recorded body did not. That is the whole")
print("  claim of a symbolic extent.")

head("WHAT IS SYMBOLIC AND WHAT IS NOT")
print("  SYMBOLIC   M, K, N -- resolved at compile, so one trace serves all")
print("  COMPILE    gm, gn, nk, part -- ordinary Python knobs, read while")
print("             tracing, so a different value is a DIFFERENT trace\n")
was = ops.matmul.traces
for gm in (2, 4, 8):
    ops.matmul.plan(
        dev.tensor(np.zeros((8, 64), np.float16)),
        dev.tensor(np.zeros((8, 64), np.float16)),
        gm=gm,
    )
print(f"  three tilings of one shape -> {ops.matmul.traces - was} new traces")
print("\n  A knob branches the trace because the body READS it; an extent does")
print("  not because the body only carries it. `kernels/mlp.py` leans on this:")
print("  `act=` picks an activation at trace time and the unchosen arms are")
print("  never recorded at all.")

head("A SHAPE THAT DOES NOT DIVIDE")
print("The machine works in 4-wide lane groups and 32-wide K-blocks. A shape")
print("that is not a multiple is PADDED by the compiler, not refused:\n")
for m, k, n in ((7, 33, 5), (1, 32, 1), (3, 64, 2)):
    a = np.asarray(rng.normal(0, 0.5, (m, k)), np.float16)
    b = np.asarray(rng.normal(0, 0.5, (n, k)), np.float16)
    got = ops.matmul(dev.tensor(a), dev.tensor(b)).numpy()
    want = a.astype(np.float64) @ b.astype(np.float64).T
    err = float(np.abs(np.asarray(got) - want).max() / np.abs(want).max())
    print(f"  {m} x {k} x {n:<4} -> shape {np.asarray(got).shape}, err {err:.2%}")
print("\n  The padding never reaches the answer. `test_short_m.py` walks M from")
print("  1 to 64 densely for exactly this reason -- the boundary is where the")
print("  padded row count stops fitting the last part.")
