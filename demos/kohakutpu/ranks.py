"""RUN THIS. `...`, so one kernel serves every rank -- and what it compiles to.

A kernel should not have to know whether its input is `(L, D)`, `(B, L, D)` or
`(B, H, L, D)`. A leading `...` says so, and means one of two things:

    In(...)          ONE CONTIGUOUS BLOCK of whatever rank arrives
    In(..., L, D)    the leading axes are a BATCH; the body describes ONE
                     element and the compiler makes the batch a grid axis

A port written WITHOUT `...` is shared by every batch element.
"""

import numpy as np
from kohakutpu.ops import matmul, silu

from kohakutpu import api as ktpu
from kohakutpu import sim

RULE = "=" * 78
rng = np.random.default_rng(0)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def err(got, want) -> float:
    return float(np.abs(np.asarray(got) - want).max() / max(np.abs(want).max(), 1e-9))


dev = ktpu.script_device("sim")

head("In(...) -- RANK-AGNOSTIC, ONE CONTIGUOUS BLOCK")
print("`silu` walks elements and never asks how they are arranged.\n")
print(f"  {'input':<20} {'result':<20} {'stages':>6} {'err':>8}")
for shape in ((64,), (8, 32), (2, 3, 8, 32), (2, 2, 2, 2, 16)):
    X = (rng.standard_normal(shape) * 2).astype(np.float16)
    Xf = X.astype(np.float64)
    got = sim.observe(silu, X)
    print(
        f"  {shape!s:<20} {got.result.shape!s:<20} {got.stages:>6} "
        f"{err(got.result, Xf / (1 + np.exp(-Xf))):>8.2e}"
    )

head("In(..., M, K) -- THE BATCH BECOMES A GRID AXIS")
print("`b` is written WITHOUT `...`, so ONE weight is shared by every batch")
print("element -- which is what applying a weight to a stack of activations is.\n")
W = (rng.standard_normal((32, 64)) * 0.5).astype(np.float16)
print(f"  {'a shape':<18} {'result':<18} {'grid':<10} {'batch':>6} {'err':>8}")
for shape in ((8, 64), (4, 8, 64), (2, 3, 8, 64)):
    A = (rng.standard_normal(shape) * 0.5).astype(np.float16)
    got = sim.observe(matmul, A, W)
    made = got.compiled
    want = A.astype(np.float64) @ W.astype(np.float64).T
    print(
        f"  {shape!s:<18} {got.result.shape!s:<18} "
        f"{made.stages[0].grid!s:<10} {made.batch!s:>6} "
        f"{err(got.result, want):>8.2e}"
    )

head("WHAT THE COMPILER MADE OF IT")
print(f"  {'a shape':<18} {'a layout':<14} {'b layout':<14} {'instances':>9}")
w = ktpu.tensor(W)
for shape in ((8, 64), (4, 8, 64), (2, 3, 8, 64)):
    made = matmul.plan(ktpu.tensor(np.zeros(shape, np.float16)), w)
    print(
        f"  {shape!s:<18} {made.layouts['a'].key:<14} "
        f"{made.layouts['b'].key:<14} {len(made.instances):>9}"
    )
print("\n  `b` keeps ONE layout and ONE upload however deep the batch is. That")
print("  is the difference between a port with `...` and a port without.")

head("THE NUMBER THAT MATTERS")
print(f"  silu   traces recorded: {silu.traces}")
print(f"  matmul traces recorded: {matmul.traces}")
print("\n  A rank is not a trace. Every shape above went through one recorded")
print("  body; only the grid and the instance count moved.")

head("WHERE `...` DOES NOT REACH")
print("A leading `...` batches. It does NOT let a kernel index across the")
print("batch, which is why GQA stacks its query heads on the ROW axis instead")
print("of asking for `q[b, h]` against `k[b, h // g]`.\n")
print("  flash_attention(q, k, v)        heads ride `...`, one per batch element")
print("  flash_attention(q, k, v, groups=2)  query heads stacked on rows")
print("\n  See demos/kohakutpu/flash_attention.py -- both cost the same 10 stages.")
