"""Two matmul defects, and which stage each lives in.

    python scripts/py/nondeterminism.py            # mesh_0, real ViT weights
    python scripts/py/nondeterminism.py --dist normal --mesh 2

Two DISJOINT sets, and only one is still a question.

`blown` is OPERAND RANGE and is closed: the count follows magnitude and nothing
else -- `--scale 0.25` takes it to zero, `--scale 2` triples it. A contraction
driven past the drain saturates at the FP16 maximum, which is the trap
`.plan/MESH0-FAULT.md` retracted a hardware narrative over.

`flickering` is ~0.5% of elements differing between runs and is OPEN. It does
not follow magnitude, and it is not placement, stale tile state, transport or
accumulation order -- `.plan/measurements/accuracy-and-defects.md` s3.4 has the
eliminations. Between runs `run1/run0` is an exact power of two in ~88% of them
with neither run correct, which points at the per-block E8M0 scale.

Operands are uploaded ONCE and read back every run, so upload is held still and
checked -- which is what separates a transport fault from a datapath one.
"""

import argparse

import numpy as np
from kohakutpu.host import Card
from kohakutpu.kernels import matmul
from kohakutpu.rt import Device
from ktpu.hw import formats

M, K, N = 128, 256, 256
TILING = {"gm": 8, "gn": 8, "nk": 2}
FP16_MAX = 65504.0


def operands(dist: str, seed: int) -> tuple:
    """`(a[M,K], bt[N,K])` in float64, for one operand distribution."""
    rng = np.random.default_rng(seed)
    if dist == "normal":
        return rng.standard_normal((M, K)), rng.standard_normal((N, K))
    if dist == "lowrank":
        r = max(1, K // 16)
        w = rng.standard_normal((r, K))
        return rng.standard_normal((M, r)) @ w, rng.standard_normal((N, r)) @ w
    from realweights import slab

    weight, prev = slab(N, K)
    act = rng.standard_normal((M, K)) @ prev.T
    return (act - act.mean(1, keepdims=True)) / act.std(1, keepdims=True), weight


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="real", choices=["real", "lowrank", "normal"])
    ap.add_argument("--mesh", type=int, default=0)
    ap.add_argument("--runs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument(
        "--scale", type=float, default=1.0, help="power of two on A; 0.25 clears blown"
    )
    args = ap.parse_args()

    a, bt = operands(args.dist, args.seed)
    a = a * args.scale
    a16, b16 = a.astype(np.float16), bt.astype(np.float16)
    want = formats.matmul_fp64(a16.astype(np.float64), b16.astype(np.float64))
    peak = np.abs(want).max()

    card = Card(which=[args.mesh])
    card.select(args.mesh)
    dev = Device(card)
    ta, tb = dev.tensor(a16), dev.tensor(b16)
    print(f"  mesh_{args.mesh}, {args.dist} operands, |want| peak {peak:.4f}\n")

    runs, drift = [], []
    for i in range(args.runs):
        runs.append(matmul(ta, tb, **TILING).numpy().astype(np.float64))
        bad = sum(
            sum(
                1
                for p, q in zip(dev.read(b.addr, b.nbytes), b.layout.pack(t.host))
                if p != q
            )
            for t in (ta, tb)
            for b in t.buffers.values()
        )
        drift.append(bad)
        d = np.abs(runs[i] - runs[0])
        print(
            f"  run {i}: {int((d > 0).sum()):,}/{d.size:,} differ from run 0, "
            f"worst abs {d.max():.3e} ({d.max() / peak:.2%} of peak)"
        )

    print(f"\n  operand bytes on the card that differ from what was sent: {drift}")

    blown = np.abs(runs[0]) > 10 * peak
    ever = sum(np.abs(r - runs[0]) > 0 for r in runs[1:]) > 0
    err = np.abs(runs[0] - want)
    keep = ~blown
    print(
        f"\n  blown (>10x peak) : {int(blown.sum()):,}/{blown.size:,}"
        f"   at the FP16 max: {int((np.abs(runs[0]) >= FP16_MAX).sum()):,}"
    )
    print(f"  flickering        : {int(ever.sum()):,}/{ever.size:,}")
    print(f"  overlap           : {int((blown & ever).sum()):,}")
    print(
        f"\n  vs FP64, blown removed: p50 {np.percentile(err[keep], 50):.3e}  "
        f"p99 {np.percentile(err[keep], 99):.3e}  max {err[keep].max():.3e}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
