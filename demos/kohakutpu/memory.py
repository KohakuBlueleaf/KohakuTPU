"""RUN THIS. The memory manager: what is resident, what is freed, and when.

An application never allocates. It controls LIFETIME, with three calls, and the
arena does the rest. Everything printed below is read from the live arena.
"""

import numpy as np

from kohakutpu import api, ops
from kohakutpu import kernels as K

RULE = "=" * 78
rng = np.random.default_rng(0)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def used() -> int:
    return api.stats()["used"]


def mb(n: int) -> str:
    return f"{n / (1 << 20):8.3f} MB"


def show(label: str) -> None:
    print(f"  {label:<44} {mb(used())}")


dev = api.script_device("sim")

head("THE THREE CONTROLS")
print("  api.tensor(a)      upload; dropped when nothing references it")
print("  .pin() / .unpin()  keep across steps, then give back")
print("  api.empty_cache()  return what nothing is using, KEEP every live one")
print(f"\n  {dev}")

api.empty_cache()
show("after empty_cache()")

head("A DROPPED TENSOR IS RECLAIMED, A PINNED ONE IS NOT")
x = np.asarray(rng.normal(0, 0.5, (256, 256)), np.float16)
w = np.asarray(rng.normal(0, 0.5, (256, 256)), np.float16)

api.empty_cache()
base = used()
weight = api.tensor(w).pin()
# An upload is LAZY -- the bytes cross the link when a kernel first wants them
# in a byte order, not when `tensor()` returns.
show("api.tensor(w).pin(), before anything reads it")
ops.matmul(api.tensor(x), weight)
show("after the first kernel read it")
resident = used()

for step in range(3):
    held = api.tensor(x * (step + 1))
    ops.matmul(held, weight)
    del held
    show(f"  step {step}: temp uploaded, matmul, temp dropped")

api.empty_cache()
show("empty_cache() with the weight still pinned")
print(f"  the weight survived: {'yes' if used() >= resident - base else 'NO'}")
print("  THREE calls, ONE upload. A weight crosses the link once however many")
print("  times it is multiplied -- which is what makes a layer loop affordable.")

weight.unpin()
api.empty_cache()
show("after unpin() + empty_cache()")

head("WHAT A KERNEL ALLOCATES BEHIND YOUR BACK")
print("`temps` are the buffers a kernel needs that are not operands. Each one is")
print("a MAG round trip you did not write.\n")
print(f"  {'kernel':<18} {'temps':>5}  {'names':<22} bytes of temp")
xs = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
ws = np.asarray(rng.normal(0, 0.3, (64, 128)), np.float16)
bias = np.asarray(rng.normal(0, 0.5, (64,)), np.float16)
res = np.zeros((64, 64), np.float16)
for name, kern, args in (
    ("matmul", ops.matmul, (xs, ws)),
    ("linear_silu", K.linear_silu, (xs, ws)),
    ("linear_bias", K.linear_bias, (xs, ws, bias)),
    ("linear_add", K.linear_add, (xs, ws, res)),
    ("softmax", ops.softmax, (xs,)),
):
    made = kern.plan(*[api.tensor(a) for a in args])
    nbytes = sum(int(np.prod(shape)) * 2 for shape in made.temps.values())
    print(
        f"  {name:<18} {len(made.temps):>5}  {sorted(made.temps)!s:<22} " f"{nbytes:,}"
    )
print("\n  `linear_bias` and `linear_add` compute the same thing. One declares its")
print("  bias `(N)` and rides the resident tile; the other takes it at full shape")
print("  and pays a temp. That is the whole per-channel argument, in bytes.")

head("THE ARENA IS FINITE, AND SAYS SO")
api.empty_cache()
print(f"  {dev}  -- filling it 128 KB at a time\n")
held = []
try:
    for _ in range(4096):
        got = api.tensor(np.zeros((256, 256), np.float16)).pin()
        ops.silu(got)
        held.append(got)
except Exception as why:  # noqa: BLE001 -- the arena names itself
    print(f"  refused after {len(held)} tensors ({mb(used())} live)")
    print(f"    {str(why)[:70]}")
print("\n  It REFUSES. It does not silently overwrite and it does not swap,")
print("  which is why a shape that does not fit is a compile-time answer.")
for t in held:
    t.unpin()
api.empty_cache()
show("released")

head("WHAT AN LLM'S WEIGHTS WOULD COST")
print("  Pinned weights per transformer layer, at a few widths:\n")
print(f"  {'D':>6} {'hidden':>7} {'params':>12} {'bytes':>12}")
for d, hidden in ((64, 256), (512, 2048), (2048, 8192), (4096, 11008)):
    params = 4 * d * d + 2 * d * hidden + 2 * d
    print(f"  {d:>6} {hidden:>7} {params:>12,} {params * 2:>12,}")
print(f"\n  This arena holds {16 << 20:,} bytes. A 2048-wide layer is 100 MB and a")
print("  4096-wide one is 314 MB, so NEITHER fits -- one layer, let alone a")
print("  stack. That is why `.plan/MEMORY-MANAGER.md` exists and why multi-mesh")
print("  scheduling is on the list rather than being an optimisation.")
