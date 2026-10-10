"""Matmul-cluster and mover primitive rates from hand-written L1 (rebuild plan L1-c).

    python scripts/py/l1/cluster_prims.py --build build/v9prof/vlt_card_v9_1n

A cluster reports no timestamps, so each rate is the DIFFERENCE of two
packages' node cycles that differ only in the measured quantity: the fixed
dispatch, fetch and completion costs cancel.
"""

import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from kohakuaccel.package import mover as PM
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm

ENTRY = 128  # bytes of one MXFP7 L1 entry
SUBTILE = 32  # bytes of one drained 4x4 fp16 sub-tile


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    a = ap.parse_args()
    card = L1Card(a.build)
    mgs = Program(card.machine).units("MG")
    vcs = Program(card.machine).units("VC")
    ops = card.put(np.zeros(256 * ENTRY * 4, np.uint8))  # A and B entries, any bytes
    out = card.alloc(4096 * SUBTILE * 4)
    big = card.put(np.zeros(1 << 16, np.float16))  # 128 KB source for the mover
    sink = card.alloc(1 << 17)

    def run(build) -> int:
        p = Program(card.machine)
        build(p)
        return card.run(p)["cycles"]

    def per(name, unit, lo, hi, make, ideal=None):
        c0, c1 = run(lambda p: make(p, lo)), run(lambda p: make(p, hi))
        d = (c1 - c0) / (hi - lo)
        tail = f"  ideal {ideal}  rate {ideal / d:.3f}" if ideal else ""
        print(
            f"{name:34s} {d:9.1f} node cycles per {unit}   ({c0} @{lo}, {c1} @{hi}){tail}",
            flush=True,
        )

    # One sweep, 64x64 sub-tiles over 2 K-blocks: 4,194,304 MACs = 4,096 cycles at 1,024.
    def sweeps(p, k, units=1):
        for u in mgs[:units]:
            p.send(u, Fill(ops, 128, sel=0), Fill(ops + 128 * ENTRY, 128, sel=1))
            for i in range(k):
                p.send(u, Gemm(64, 64, 2, acc=i > 0))
            p.send(u, Drain(out, 1))
        p.barrier()

    per("GEMM 64x64x2 sweep, 1 cluster", "sweep", 1, 9, sweeps, ideal=4096)
    per(
        "GEMM 64x64x2 sweep, 4 clusters",
        "sweep (each)",
        1,
        9,
        lambda p, k: sweeps(p, k, 4),
        ideal=4096,
    )

    def fills(p, n, units):
        for u in mgs[:units]:
            p.send(u, Fill(ops, n, sel=0))
        p.barrier()

    for units in (1, 2, 4):
        per(
            f"FILL, {units} cluster(s) at once",
            "entry (each)",
            32,
            224,
            lambda p, n, u=units: fills(p, n, u),
        )

    def drains(p, n, units=1, node=False):
        for k, u in enumerate(mgs[:units]):
            p.send(
                u,
                Fill(ops, 128, sel=0),
                Fill(ops + 128 * ENTRY, 128, sel=1),
                Gemm(64, 64, 2),
            )
            if node:
                p.send(u, Drain(0, n, dst=vcs[0]))
            else:
                p.send(u, Drain(out + k * 4096 * SUBTILE, n))
        p.barrier()

    per("DRAIN to memory, 1 cluster", "sub-tile", 64, 4096, drains)
    per(
        "DRAIN to memory, 4 clusters",
        "sub-tile (each)",
        64,
        4096,
        lambda p, n: drains(p, n, 4),
    )
    per(
        "DRAIN to a vector core's L1",
        "sub-tile",
        16,
        256,
        lambda p, n: drains(p, n, 1, True),
    )

    def copies(p, nbytes):
        p.move(PM.copy(big, sink, nbytes))

    def quants(p, nbytes):
        p.move(PM.convert(big, sink, nbytes // 256))

    per("mover copy", "KB", 8 << 10, 64 << 10, lambda p, n: copies(p, n), None)
    per(
        "mover FP16->MXFP7 quantise",
        "KB of source",
        8 << 10,
        64 << 10,
        lambda p, n: quants(p, n),
        None,
    )
    card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
