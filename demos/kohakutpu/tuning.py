"""RUN THIS. The tiling knobs: what they change, what they cost, what they refuse.

`gm`, `gn` and `nk` are literally fields of the GEMM instruction -- sub-tile rows
per instance, sub-tile columns per instance, K-blocks per sweep. They change the
grid, the statements per instance and the byte order the operands need, and NOT
one line of the kernel. That is what "arch-agnostic" means here.
"""

import numpy as np
from kohakutpu.ops import matmul

from kohakutpu import api as ktpu
from kohakutpu import sim

RULE = "=" * 78
rng = np.random.default_rng(0)
M, K, N = 32, 128, 64
A = (rng.standard_normal((M, K)) * 0.5).astype(np.float16)
B = (rng.standard_normal((N, K)) * 0.5).astype(np.float16)
want = A.astype(np.float64) @ B.astype(np.float64).T


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


dev = ktpu.script_device("sim")

head("ONE KERNEL, MANY TILINGS")
print(f"  {dev}")
print(
    f"  {len(dev.machine.units['MG'])} clusters, "
    f"{len(dev.machine.units['VC'])} vector cores"
)
print(f"  matmul at M={M} K={K} N={N}\n")
print(
    f"  {'gm gn nk':<10} {'grid':<10} {'inst':>5} {'stmt':>5} "
    f"{'a layout':>12} {'cycles':>16} {'err':>8}"
)

worst = 0.0
for gm, gn, nk in (
    (1, 1, 1),
    (2, 1, 2),
    (2, 2, 2),
    (4, 1, 4),
    (1, 4, 2),
    (8, 8, 2),
    (4, 8, 4),
):
    a, b = ktpu.tensor(A), ktpu.tensor(B)
    got = matmul(a, b, gm=gm, gn=gn, nk=nk)
    err = float(np.abs(got.numpy() - want).max() / np.abs(want).max())
    worst = max(worst, err)

    made = matmul.plan(a, b, gm=gm, gn=gn, nk=nk)
    timing = sim.cycles(matmul, A, B, gm=gm, gn=gn, nk=nk)
    print(
        f"  {gm} {gn} {nk:<6} {made.grid!s:<10} {len(made.instances):>5} "
        f"{len(made.instances[0].stmts):>5} {made.layouts['a'].key:>12} "
        f"{timing.overlapped:>7,}-{timing.cycles:<8,} {err:>7.2%}"
    )
    ktpu.empty_cache()

print(f"\n  Same kernel, seven tilings, worst error {worst:.2%} -- every one of")
print("  them computes the same thing. The knob is a SPEED decision, and the")
print("  spread above is the whole reason `tiling.plan` exists.")

head("WHAT THE KNOB ACTUALLY MOVES")
print("  gm x gn is the accumulator's sub-tile shape, so it sets BOTH the")
print("  instance count and the L1 footprint. nk is how many 32-wide K-blocks")
print("  one sweep contracts, so it trades fills against sweep length.\n")
for gm, gn, nk in ((2, 2, 2), (8, 8, 2), (8, 8, 8)):
    made = matmul.plan(ktpu.tensor(A), ktpu.tensor(B), gm=gm, gn=gn, nk=nk)
    kinds: dict = {}
    for s in made.instances[0].stmts:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    print(
        f"  gm={gm} gn={gn} nk={nk:<3} instances={len(made.instances):<4} "
        f"per instance: {kinds}"
    )

head("WHERE IT REFUSES, AND WHY")
print("A PLAIN matmul constrains almost nothing -- it drains through MAG, so a wide")
print("tile is merely slow. The constraints bite once an epilogue has to ride")
print("the tile, because then it must fit a vector core's L1.\n")
from kohakutpu import kernels as KER

for label, kern, knobs in (
    ("matmul   gm=64 gn=64", matmul, {"gm": 64, "gn": 64, "nk": 2}),
    ("matmul   nk=64", matmul, {"gm": 8, "gn": 8, "nk": 64}),
    ("matmul   gm=0", matmul, {"gm": 0, "gn": 8, "nk": 2}),
    ("linear_silu gm=16 gn=32", KER.linear_silu, {"gm": 16, "gn": 32, "nk": 2}),
    ("linear_silu gm=1 gn=1", KER.linear_silu, {"gm": 1, "gn": 1, "nk": 2}),
):
    try:
        made = kern.plan(ktpu.tensor(A), ktpu.tensor(B), **knobs)
        note = f"accepted, fuse={made.knobs.get('fuse', True)}"
        if made.temps:
            note += f", staged into {sorted(made.temps)}"
        print(f"  {label:<24} {note}")
    except Exception as why:  # noqa: BLE001 -- the compiler names itself
        print(f"  {label:<24} REFUSED: {str(why).split(';')[0][:56]}")
print("\n  A knob that cannot fuse is STAGED, not refused. A knob that is not a")
print("  positive integer is refused by name -- it used to be a ZeroDivisionError")
print("  from inside `ceildiv`, which told the author nothing.")

head("THE DEFAULTS ARE CHOSEN, NOT GUESSED")
print("`tiling.plan` prices the shape against the machine. It is what the")
print("tinygrad frontend asks too, so both rungs get the same answer.\n")
from ktpugrad.tiling import plan as pick

for label, fn in (("matmul", matmul), ("linear_silu", KER.linear_silu)):
    for m, n in ((32, 64), (2, 6144), (256, 256)):
        _, knobs = pick(
            fn, m, n, len(dev.machine.units["VC"]), k=K, machine=dev.machine
        )
        print(f"  {label:<12} M={m:<5} N={n:<6} -> {knobs}")
print("\n  READ THAT AGAIN: the knobs DO NOT MOVE with the shape. `tiling.plan`")
print("  keeps the kernel's own defaults wherever they fit, and it reports")
print("  `fuse=True` even at M=2 N=6144, where 768 tiles cannot reach 4 vector")
print("  cores. The adaptation happens LATER, in `Kernel.relax`, which retries")
print("  with `fuse=False` once the fused build actually raises.")
print("\n  That split is worth knowing before trusting either half: `plan` says")
print("  what to TRY, and only the compile says what happened. The `fuse=` in")
print("  the table above is a request, not a result -- the section before this")
print("  one shows the same shapes coming back staged.")
