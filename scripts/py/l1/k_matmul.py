"""Card run of the L1 matmul / linear+bias (`kohakutpu.ir.l1.kernels.matmul`).

    python scripts/py/l1/k_matmul.py --build build/v9prof/vlt_card_v9_1n --shape 1024,1024,1024 --tile 64,64,2 [--bias]

Each case runs twice (the second is reported, operands warm in the Xache) and
is graded against the MXFP7-quantised reference. `--parts` drops fills (f),
sweeps (s) or drains (d) to measure the rest.
"""

import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from check import grade
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import fused as FU
from kohakutpu.ir.l1.kernels import matmul as MM


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--shape", action="append", default=[], help="M,K,N")
    ap.add_argument("--tile", action="append", default=[], help="gm,gn,nk")
    ap.add_argument("--parts", action="append", default=[], help="subset of fsd")
    ap.add_argument("--bias", action="store_true", help="linear + per-channel bias")
    ap.add_argument(
        "--plan",
        action="append",
        default=[],
        choices=("rr", "stagger"),
        help="tile plan: round robin, or staggered column bands",
    )
    ap.add_argument(
        "--epilogue",
        action="append",
        default=[],
        choices=("silu", "silu-late"),
        help="silu on each drained tile by the vector cores, overlapped (fused.py); "
        "-late keeps the late fused DRAIN",
    )
    a = ap.parse_args()
    variants = [(p, plan) for plan in (a.plan or ["rr"]) for p in (a.parts or ["fsd"])]
    variants += [("fsd", e) for e in a.epilogue]
    shapes = [tuple(int(v) for v in s.split(",")) for s in a.shape] or [(512, 512, 512)]
    tiles = [tuple(int(v) for v in s.split(",")) for s in a.tile] or [(64, 64, 2)]
    card = L1Card(a.build)
    rng = np.random.default_rng(7)
    for m, k, n in shapes:
        x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
        w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
        bias = (rng.standard_normal(n) * 4).astype(np.float16)
        want = x.astype(np.float64) @ w.astype(np.float64).T
        model = value_fp16(x) @ value_fp16(w).T
        if a.bias:
            want = want + bias.astype(np.float64)
            model = (
                model
                + value_fp16(np.pad(bias[:, None], ((0, 0), (0, MM.KBLOCK - 1))))[:, 0]
            )
        for gm, gn, nk in tiles:
            t0 = time.monotonic()
            card.top = 0x0010_0000
            a_at = card.put(np.frombuffer(MM.pack_a(x, gm, nk), np.uint8))
            b_at = card.put(np.frombuffer(MM.pack_b(w, gn, nk), np.uint8))
            c_at = card.alloc(m * n * 2)
            sinks = [card.alloc(256 * 32) for _ in range(2)]
            extra = {}
            if a.bias:
                extra = {
                    "ones_at": card.put(np.frombuffer(MM.ones_block(gm), np.uint8)),
                    "bias_at": card.put(
                        np.frombuffer(MM.bias_block(bias, gn), np.uint8)
                    ),
                }
            for parts, plan in variants:
                fused = plan.startswith("silu")
                try:
                    prog = Program(card.machine)
                    if fused:
                        where = FU.matmul_silu(
                            prog,
                            a_at,
                            b_at,
                            c_at,
                            m,
                            n,
                            k,
                            gm,
                            gn,
                            nk,
                            late=plan == "silu-late",
                            sinks=sinks,
                        )
                    else:
                        where = MM.matmul(
                            prog,
                            a_at,
                            b_at,
                            c_at,
                            m,
                            n,
                            k,
                            gm,
                            gn,
                            nk,
                            parts,
                            stagger=plan == "stagger",
                            **extra,
                        )
                except ValueError as exc:
                    print(f"{m}x{k}x{n} tile {gm}x{gn}x{nk} {plan}: {exc}")
                    continue
                card.write(c_at, np.zeros(m * n, np.float16))
                cold = card.run(prog)["cycles"]
                got = card.run(prog)["cycles"]
                eff = m * n * k / (1024 * len(prog.units("MG")) * got)
                print(
                    f"{m}x{k}x{n} tile {gm}x{gn}x{nk} {plan} parts {parts}: {got} "
                    f"cycles (cold {cold})  MAC {eff:.3f}  "
                    f"({time.monotonic() - t0:.0f}s)",
                    flush=True,
                )
                if parts == "fsd":
                    act = (lambda v: v / (1 + np.exp(-v))) if fused else (lambda v: v)
                    grade(
                        f"{m}x{k}x{n} tile {gm}x{gn}x{nk} {plan}",
                        MM.unpack(card.get, where, m, n, gm, gn),
                        act(model),
                        act(want),
                    )
    card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
