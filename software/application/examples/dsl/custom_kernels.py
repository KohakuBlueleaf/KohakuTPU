"""Your own DSL kernel, then tinygrad around it, on the KohakuTPU simulator.

    python software/application/examples/dsl/custom_kernels.py
    KOHAKUTPU_SIM_NODES=2 python software/application/examples/dsl/custom_kernels.py --nodes 2

1. `gated_rmsnorm` (``kt``): rmsnorm, weight and SiLU gate in one vector-core
   kernel; its L2 trace printed.
2. tinygrad makes its input, `ktpugrad.bridge` runs it on those Tensors,
   tinygrad carries on -- all on the card, checked against numpy.
3. The same kernel through the Tensor API (`kp.function`).
4. Two nodes, both ways: `linear` shares memory through the Xache (no
   cross-node code), `splitk` adds into node 0's accumulators
   (`kt.drain(acc, kt.node[0].mg[u], add=True)`); L2 printed, run on ``--nodes 2``.
"""

import argparse

import numpy as np
from tinygrad.tensor import Tensor

import kohakutpu.language.dsl as kt
from kohakutpu.application import api as kp
from kohakutpu.application import ktpugrad
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.call import call
from kohakutpu.language.examples import dsl as ex


@kt.kernel
def gated_rmsnorm(
    x=kt.In("R", "W"),
    g=kt.In("W"),
    z=kt.In("R", "W"),
    y=kt.Out("R", "W"),
    *,
    eps=1e-6,
    rows=kt.auto(8, 4),
):
    """y = rmsnorm(x) * g * silu(z), one row band a trip on every core."""
    R, W = x.shape
    cores = kt.machine().vc
    kt.require(
        W % 32 == 0 and W <= 256, f"rows of whole 32-column blocks up to 256, not {W}"
    )
    kt.require(R % (cores * rows) == 0, f"{R} rows over {cores} cores x {rows}")
    per = R // cores
    for c in kt.par(cores, on=kt.vc):
        gl = kt.unpack(kt.load(g, gt4=True))  # one row, loaded once a core
        for t in kt.range(per // rows):
            r = c * per + t * rows
            v = kt.load(x[r : r + rows, :], gt4=True)
            zv = kt.load(z[r : r + rows, :], gt4=True)
            inv = kt.rsqrt(kt.rsum(v * v) * (1.0 / W) + eps)
            gate = zv * kt.recip(1.0 + kt.exp2(zv * -kt.LOG2E))
            kt.store(y[r : r + rows, :], v * inv * gl * gate)


def reference(x, g, z, eps=1e-6) -> np.ndarray:
    xd, gd, zd = (np.asarray(a, np.float64) for a in (x, g, z))
    rms = np.sqrt((xd * xd).mean(-1, keepdims=True) + eps)
    return xd / rms * gd * (zd / (1 + np.exp(-zd)))


def rel(got, want) -> float:
    got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
    return float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-9))


def softmax(a):
    e = np.exp(a - a.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument(
        "--nodes",
        type=int,
        default=1,
        help="run the multi-node kernels (needs a card of that many nodes)",
    )
    args = ap.parse_args()
    rng = np.random.default_rng(0)
    R, W = 64, 128
    x0 = rng.standard_normal((R, W)).astype(np.float16)
    w = (rng.standard_normal((W, W)) / np.sqrt(W)).astype(np.float16)
    g = (rng.standard_normal(W) * 0.5 + 1).astype(np.float16)
    z = rng.standard_normal((R, W)).astype(np.float16)

    print("== 1. the kernel's L2 trace ==")
    print(
        gated_rmsnorm.trace(
            {"x": ((R, W), "f16"), "g": ((W,), "f16"), "z": ((R, W), "f16")}
        ).text
    )

    print("== 2. inside a tinygrad program ==")
    xt = Tensor(x0, device="KTPU") @ Tensor(w, device="KTPU").T  # tinygrad's product
    y = ktpugrad.bridge(gated_rmsnorm)(
        xt, Tensor(g, device="KTPU"), Tensor(z, device="KTPU")
    )
    out = (y @ Tensor(w, device="KTPU").T).softmax(-1)  # tinygrad again
    xv = np.asarray(xt.numpy(), np.float64)
    want = softmax(reference(xv, g, z) @ np.asarray(w, np.float64).T)
    print(f"  rel err vs numpy {rel(out.numpy(), want):.2e} (MXFP7 products)")

    print("== 3. through the Tensor API ==")
    f = kp.function(gated_rmsnorm)
    ya = (f(kp.tensor(x0), kp.tensor(g), kp.tensor(z)) * 2.0 + 1.0).sum(-1)
    print(
        f"  rel err vs numpy {rel(ya.numpy(), (reference(x0, g, z) * 2 + 1).sum(-1)):.2e}"
    )
    dev = D.device()
    print(
        f"  {len(dev.runs)} kernel runs so far, {sum(r.cycles for r in dev.runs):,} card cycles"
    )

    print("== 4. several nodes ==")
    two = kt.Machine(mg=4, vc=2, nodes=2)
    lin, spk = ex.EXAMPLES["linear"], ex.EXAMPLES["splitk"]
    print("-- linear on 2 nodes (memory shared through the Xache) --")
    print(lin.kernel.trace(lin.given, machine=two, **lin.consts).text)
    print("-- splitk on 2 nodes (node 1 adds into node 0's accumulators) --")
    print(spk.kernel.trace(spk.given, machine=two, **spk.consts).text)
    if args.nodes > 1:
        if dev.nodes < args.nodes:
            raise SystemExit(
                f"the card has {dev.nodes} node(s): set KOHAKUTPU_SIM_NODES={args.nodes}"
            )
        for e in (lin, spk):
            ins = e.inputs(rng)
            outs, run = call(dev, e.kernel, ins, **e.consts)
            print(
                f"  {e.kernel.name} on {dev.nodes} nodes: rel {rel(outs[e.out], e.reference(ins)):.2e}, {run.cycles:,} cycles"
            )


if __name__ == "__main__":
    main()
