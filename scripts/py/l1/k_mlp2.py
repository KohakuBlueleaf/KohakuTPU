"""Card run of ``silu(a @ b.T)`` with a V2 vector epilogue, overlapped in one
package, on a card model built with V2 vector cores (`card_v9_1n_v2`).

    python scripts/py/l1/k_mlp2.py --build build/v9v2/vlt_card_v9_1n_v2 \
        --shape 1024,1024,1024 [--tile 64,64,2] [--plan rr|stagger]

The clusters run `matmul.cluster_ops`; each drained tile is a `mark` in its
cluster's stream, and once the node has waited up to it (one round of tiles
behind the sends, as `kernels/fused.py`) a vector core runs the V2 streaming
silu (`vec2/kernels.py` `stream`) over the tile in memory, in place. Tiles go
to the cores in drain order, round robin. A core's image is sent once; every
later tile is its descriptors and a RUN. Each case runs twice, the second
reported (operands warm, images resident).
"""

import argparse
import pathlib
import sys
import time

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "vec2"))
import kernels
from card import L1Card
from check import grade
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.vector2 import Kernel

#: Words a V2 silu RUN covers per loop pass: two 128-word tiles.
PASS = 256


def mlp(prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, stagger) -> list:
    """Queue ``c = silu(a @ b.T)``; returns `matmul`'s tile list."""
    cores = prog.units("VC")
    clusters = len(prog.units("MG"))
    ready, where, turn = [], [], 0
    events = list(
        MM.cluster_ops(prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, stagger=stagger)
    )

    def epilogue(upto: int) -> None:
        nonlocal turn
        while ready and ready[0][0] <= upto:
            _, u, tok, tile = ready.pop(0)
            prog.wait(u, tok)
            at, span = tile[3], tile[1] * gn
            if span % PASS:
                raise ValueError(f"a {span}-word tile is not whole {PASS}-word passes")
            build, _, _ = kernels.stream("silu", n=span * 16)
            prog.send(cores[turn % len(cores)], Kernel(build.at(at, 0, at)))
            turn += 1

    for i, (u, ops, drained) in enumerate(events):
        prog.send(u, *ops)
        if drained:
            tok = prog.mark(u)
            ready.extend((i, u, tok, tile) for tile in drained)
            where += drained
        epilogue(i - clusters)
    epilogue(len(events))
    prog.barrier()
    return sorted(where, key=lambda t: t[3])


def epilogue_only(prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, stagger) -> list:
    """The same tiles' V2 silu RUNs with no cluster work: the vector side alone."""
    cores = prog.units("VC")
    scratch = Program(prog.machine)
    tiles = [
        t
        for _, _, drained in MM.cluster_ops(
            scratch, a_at, b_at, c_at, m, n, k, gm, gn, nk, stagger=stagger
        )
        for t in drained
    ]
    for turn, tile in enumerate(tiles):
        at = tile[3]
        build, _, _ = kernels.stream("silu", n=tile[1] * gn * 16)
        prog.send(cores[turn % len(cores)], Kernel(build.at(at, 0, at)))
    prog.barrier()
    return sorted(tiles, key=lambda t: t[3])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--shape", action="append", default=[], help="M,K,N")
    ap.add_argument("--tile", default="64,64,2", help="gm,gn,nk")
    ap.add_argument("--plan", action="append", default=[], choices=("rr", "stagger"))
    ap.add_argument(
        "--steps", action="store_true", help="print each package step's end"
    )
    ap.add_argument(
        "--vector-only", action="store_true", help="the epilogue RUNs alone"
    )
    a = ap.parse_args()
    shapes = [tuple(int(v) for v in s.split(",")) for s in a.shape] or [
        (1024, 1024, 1024)
    ]
    gm, gn, nk = (int(v) for v in a.tile.split(","))
    card = L1Card(a.build, steps=a.steps)
    rng = np.random.default_rng(7)
    for m, k, n in shapes:
        x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
        w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
        model = value_fp16(x) @ value_fp16(w).T
        want = x.astype(np.float64) @ w.astype(np.float64).T
        card.top = 0x0010_0000
        a_at = card.put(np.frombuffer(MM.pack_a(x, gm, nk), np.uint8))
        b_at = card.put(np.frombuffer(MM.pack_b(w, gn, nk), np.uint8))
        c_at = card.alloc(m * n * 2 + PASS * 32 * 2)  # a pass of read past the end
        for plan in a.plan or ["rr"]:
            t0 = time.monotonic()
            got = {}
            for run in ("cold", "warm"):
                prog = Program(card.machine)
                where = (epilogue_only if a.vector_only else mlp)(
                    prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, plan == "stagger"
                )
                res = card.run(prog)
                got[run] = res["cycles"]
            for first, last, at, what in res["steps"]:
                print(f"    step {first}..{last} ends {at}: {what[:160]}")
            eff = m * n * k / (1024 * len(prog.units("MG")) * got["warm"])
            print(
                f"mlp {m}x{k}x{n} tile {gm}x{gn}x{nk} {plan}: {got['warm']} cycles "
                f"(cold {got['cold']})  MAC {eff:.3f}  ({time.monotonic() - t0:.0f}s)",
                flush=True,
            )
            if a.vector_only:
                continue
            grade(
                f"mlp {m}x{k}x{n} {plan}",
                MM.unpack(card.get, where, m, n, gm, gn),
                model / (1 + np.exp(-model)),
                want / (1 + np.exp(-want)),
            )
    card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
