"""The contraction split across two meshes, combined by a remote DRAIN.

    python scripts/py/ksplit_matmul.py [--src 1 --dst 3]

mesh_dst computes the upper half of K and KEEPS it in its accumulator; mesh_src
computes the lower half and drains it across with `dbuf=2`, adding into that
open tile at full accumulator precision. Reduce-scatter's core step.

Measured 2026-08-13 at 32x256x64: the sum matches a single-mesh whole-K run to
4.88e-04, ONE FP16 ULP -- the partial never rounds to FP16 in between. Compute
1.76x on two meshes, wall 1.45x worse. `dbuf=2` ADDS and tile memory has no
reset, so the receiver must have run a GEMM first; `matmul_reduce` orders that
(isa/cluster.md s9.4).
"""

import argparse

import numpy as np
from kohakuaccel.device.registers import MAG_BASE
from kohakutpu.host import Card
from kohakutpu.kernels import matmul
from kohakutpu.meshes import MeshGroup, Pin
from ktpu.hw import interlink as IL

M, K, N = 32, 256, 64
TILING = {"gm": 8, "gn": 16, "nk": 2}


class Board:
    """What `ktpu.hw.interlink` wants of a board: a name and `ctrl(offset)`."""

    def __init__(self, mesh) -> None:
        self.name = f"mesh_{mesh.index}"

    @staticmethod
    def ctrl(off: int) -> int:
        return MAG_BASE + off


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=int, default=0)
    ap.add_argument("--dst", type=int, default=2)
    args = ap.parse_args()

    card = Card(which=[args.src, args.dst])
    group = MeshGroup.open(card, [args.src, args.dst])
    send, recv = group[0], group[1]
    sender, fin = send.mesh.coords("MG")[1], recv.mesh.coords("MG")[3]
    print(
        f"{card}\n{group}\n{M}x{K}x{N}, K split {K // 2}+{K // 2}, "
        f"mesh_{args.src}{sender} -> mesh_{args.dst}{fin}\n"
    )

    rng = np.random.default_rng(23)
    a = rng.normal(0, 0.02, (M, K)).astype(np.float16)
    b = rng.normal(0, 1.00, (N, K)).astype(np.float16)
    f32 = np.float32
    want = a.astype(f32) @ b.astype(f32).T
    half = K // 2
    P0 = a[:, :half].astype(f32) @ b[:, :half].astype(f32).T

    with Pin(send, sender):
        y_one = matmul(send.tensor(a), send.tensor(b), **TILING).numpy()
    e = np.abs(y_one - want)
    print(
        f"  whole K on mesh_{args.src}: p50 {np.median(e):.4e} "
        f"p99 {np.percentile(e, 99):.4e}"
    )

    sb = Board(send.mesh)
    before = IL.counters(send.ctrl, sb)["link1"]
    y = group.matmul_reduce(
        group.split(a, 1),
        group.split(b, 1),
        into=1,
        tile=fin,
        nodes={0: sender},
        **TILING,
    ).numpy()
    after = IL.counters(send.ctrl, sb)["link1"]
    print(
        f"  remote drain: +{after['tx_packets'] - before['tx_packets']} packets, "
        f"+{after['tx_beats'] - before['tx_beats']} beats, "
        f"faults {IL.faults(send.ctrl, sb)}"
    )

    for label, ref in (("whole contraction", want), ("one partial", P0)):
        d = np.abs(y - ref)
        print(
            f"  vs {label:18}: p50 {np.median(d):.4e}  p99 {np.percentile(d, 99):.4e}"
        )
    d = np.abs(y - y_one)
    print(
        f"  vs the single-mesh run: p99 {np.percentile(d, 99):.4e}  "
        f"identical {int((d == 0).sum()):,}/{d.size:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
