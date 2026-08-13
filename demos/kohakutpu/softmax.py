"""RUN THIS. Softmax staged vs fused, and what the byte order costs.

Two ways to write the same arithmetic. The staged one is four passes with a
full-shape temp between each; the fused one is ONE, with every intermediate in
a vector register. The output is the whole argument for the fused shape.
"""

import numpy as np
from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel

from kohakutpu import api as ktpu
from kohakutpu import kernels as K
from kohakutpu import lang as L
from kohakutpu import ops as O
from kohakutpu import sim

M, N = dims("M, N")
LOG2E = 1.4426950408889634
RULE = "=" * 78
rng = np.random.default_rng(0)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


@kernel
def softmax_staged(x=L.In(..., M, N), y=L.Out(..., M, N)):
    """One pass per intermediate, each landing behind MAG.

    `exp2` is the transcendental this core has, so the shift into log2 space
    rides on the subtraction rather than costing a pass of its own.
    """
    hi, ex, total = L.temp(M, N), L.temp(M, N), L.temp(M, N)
    hi <<= L.row_max(x)
    ex <<= L.exp2((x - hi) * LOG2E)
    total <<= L.row_sum(ex)
    y <<= ex / total


@kernel
def softmax_fused(x=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
    """The same arithmetic in ONE tiling. `ex` is read twice, so it is one
    lifted chain held in a register and neither fold reaches memory."""
    with units(x.parts(part)) as e:
        ex = L.exp2((x[e] - L.row_max(x[e])) * LOG2E)
        y[e] <<= ex / L.row_sum(ex)


dev = ktpu.script_device("sim")
rows, cols = 64, 128
X = np.asarray(rng.normal(0, 2.0, (rows, cols)), np.float16)
Xf = X.astype(np.float64)
want = np.exp(Xf - Xf.max(1, keepdims=True))
want /= want.sum(1, keepdims=True)

head("THE TWO FORMS")
print(f"  {dev}")
print(f"  softmax over {rows} rows of {cols}\n")
print(
    f"  {'form':<10} {'stages':>6} {'temps':>5} {'temp bytes':>11} "
    f"{'cycles':>16} {'err':>9}"
)
for name, kern in (("staged", softmax_staged), ("fused", softmax_fused)):
    got = sim.observe(kern, X)
    nbytes = sum(int(np.prod(s)) * 2 for s in got.compiled.temps.values())
    err = float(np.abs(got.result.astype(np.float64) - want).max() / want.max())
    print(
        f"  {name:<10} {got.stages:>6} {len(got.temps):>5} {nbytes:>11,} "
        f"{got.timing.overlapped:>7,}-{got.timing.cycles:<8,} {err:>9.2e}"
    )
print("\n  Four stages against one, and the fused form is MORE accurate: its")
print("  intermediates never round-trip through fp16 memory.")

head("THE STAGE LIST, SPELLED OUT")
for name, kern in (("staged", softmax_staged), ("fused", softmax_fused)):
    made = kern.plan(ktpu.tensor(X))
    print(f"  {name}:")
    for s in made.stages:
        kinds = [x.kind for x in (s.instances[0].stmts if s.instances else [])]
        print(f"    stage {s.index} {s.unit} grid{s.grid} {kinds}")
    print(f"    temps {sorted(made.temps)}\n")

head("WHAT THE SHIPPED KERNEL IS")
print("  `kohakutpu.kernels.softmax` IS the fused one -- one implementation per")
print("  kernel, so a caller cannot pick the slow one by accident.\n")
got = sim.observe(K.softmax, X)
err = float(np.abs(got.result.astype(np.float64) - want).max() / want.max())
print(f"  K.softmax    {got.stages} stage(s), {len(got.temps)} temps, err {err:.2e}")
got = sim.observe(O.softmax, X)
print(f"  ops.softmax  {got.stages} stage(s)  (the same kernel, at the op level)")

head("THE RELAYOUT -- why layout is a level of its own")
print("Run softmax on a host array and there is NO relayout: the tensor is")
print("already in row order. Run it on the OUTPUT of a matmul and the compiler")
print("inserts one, because a cluster drains 4x4 sub-tiles and a reduction runs")
print("ALONG a row.\n")
W = np.asarray(rng.normal(0, 0.3, (cols, cols)), np.float16)
before = dict(ktpu.stats())
K.softmax(ktpu.tensor(X)).numpy()
direct = ktpu.stats()["relayouts"] - before["relayouts"]

before = dict(ktpu.stats())
K.softmax(O.matmul(ktpu.tensor(X), ktpu.tensor(W))).numpy()
after_mm = ktpu.stats()["relayouts"] - before["relayouts"]
print(f"  softmax(host array)      {direct} relayout(s)")
print(f"  softmax(matmul(x, w))    {after_mm} relayout(s)")
print("\n  A relayout is a round trip THROUGH THE HOST -- this machine has no")
print("  instruction that reorders memory. Elementwise work commutes with the")
print("  permutation and a reduction does not, which is the whole asymmetry.")

head("A ROW WIDER THAN VLMAX")
print("`VRED` folds exactly the lanes VL names, and VLMAX is 128. A wider row")
print("is REFUSED by name, not silently folded in halves:\n")
wide = np.asarray(rng.normal(0, 1.0, (8, 512)), np.float16)
try:
    K.softmax(ktpu.tensor(wide)).numpy()
    print("  512-wide: accepted")
except Exception as why:  # noqa: BLE001 -- the kernel names itself
    print(f"  512-wide: REFUSED -- {str(why).split('.')[0][:66]}")
print("\n  What IS the way round: `K.softmax_wide`, which folds hierarchically.")
split = K.split(512)
got = K.softmax_wide(ktpu.tensor(wide.reshape(-1, 512)), rows=split)
ref = np.exp(wide.astype(np.float64) - wide.astype(np.float64).max(1, keepdims=True))
ref /= ref.sum(1, keepdims=True)
err = float(np.abs(np.asarray(got.numpy()) - ref).max() / ref.max())
print(f"  K.softmax_wide(x, rows=K.split(512)) -> err {err:.2e}")
