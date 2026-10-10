"""Vector-core primitive rates from hand-written L1 programs (rebuild plan L1-c).

    python scripts/py/l1/primitives.py --build build/v9prof/vlt_card_v9_1n

Each case is ONE image run once on the first vector core; its time is the
core's own RUN start -> halt (VC_STATE_PROF VRUN stamps, vector clock). The
rate is reported against the ideal for that instruction mix, so a low number is
a hardware fact about that primitive, not a scheduling choice.
"""

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card
from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.vector import (
    Alu,
    Bar,
    Desc,
    Dims,
    Halt,
    Image,
    Run,
    Seti,
    Setmode,
    Setvl,
    Vdrain,
    Vfill,
    Vld,
    Vshuf,
    Vst,
)

S_VL, S_ROT = 0, 1
AD_L1, AD_MEM = 0, 1
N = 64  # instructions measured per case


def head(vl=V.VLMAX, mode=V.FLAT) -> list:
    return [Seti(S_VL, vl), Seti(S_ROT, 1), Setvl(S_VL), Setmode(mode)]


def cases(src: int, dst: int) -> dict:
    """name -> (image body, descriptor ops, ideal cycles)."""
    beats = V.VLMAX // V.LANES
    out: dict = {}
    # ALU: independent ops, rotating destinations; one beat per 16 lanes.
    out["alu_indep_flat"] = (
        [Alu("VADD", vd=1 + i % 12, va=0, vc=0) for i in range(N)],
        [],
        N * beats,
    )
    # ALU: each op reads the previous one's result -- the latency, exposed.
    out["alu_dep_flat"] = (
        [Alu("VADD", vd=1 + (i + 1) % 2, va=1 + i % 2, vc=0) for i in range(N)],
        [],
        N * beats,
    )
    out["alu_indep_exp2"] = (
        [Alu("VEXP2", vd=1 + i % 12, va=0) for i in range(N)],
        [],
        N * beats,
    )
    out["alu_indep_fma"] = (
        [Alu("VFMA", vd=1 + i % 12, va=0, vb=0, vc=0) for i in range(N)],
        [],
        N * beats,
    )
    l1 = [Desc(AD_L1, 0), Dims(AD_L1, ((1, beats),))]
    out["vld"] = (
        [Vld(1 + i % 12, AD_L1, (i % 16) * beats) for i in range(N)],
        l1,
        N * beats,
    )
    out["vst"] = (
        [Vst(1 + i % 12, AD_L1, 256 + (i % 16) * beats) for i in range(N)],
        l1,
        N * beats,
    )
    out["vld_alu_overlap"] = (
        [
            x
            for i in range(N // 2)
            for x in (
                Vld(1 + i % 6, AD_L1, (i % 16) * beats),
                Alu("VADD", vd=8 + i % 6, va=0, vc=0),
            )
        ],
        l1,
        (N // 2) * beats,
    )
    out["vshuf"] = ([Vshuf(1 + i % 12, 0, S_ROT) for i in range(N)], l1, N * beats)
    out["vshuf_after_alu"] = (
        [
            x
            for i in range(N // 2)
            for x in (
                Alu("VADD", vd=1 + i % 6, va=0, vc=0),
                Vshuf(8 + i % 6, 1 + i % 6, S_ROT),
            )
        ],
        l1,
        N * beats,
    )
    # Memory: one fill / one drain of W words; ideal one word a cycle.
    for w in (64, 256):
        mem = [Desc(AD_MEM, src), Dims(AD_MEM, ((V.WORD_BYTES, w),))]
        out[f"vfill_{w}"] = ([Vfill(AD_MEM, 0), Bar()], mem, w)
        dmem = [Desc(AD_MEM, dst), Dims(AD_MEM, ((V.WORD_BYTES, w),))]
        out[f"vdrain_{w}"] = ([Vdrain(AD_MEM, 0)], dmem, w)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    card = L1Card(a.build)
    src = card.put(np.arange(256 * 16, dtype=np.float16))
    dst = card.alloc(256 * 32)
    vc = Program(card.machine).units("VC")[0]
    table = cases(src, dst)
    order = []
    for name, (body, descs, ideal) in table.items():
        p = Program(card.machine)
        p.send(vc, Image(tuple(head() + body + [Halt()])), *descs, Run(0))
        p.wait(vc)
        got = card.run(p)
        order.append((name, ideal, got["cycles"]))
    stats = card.close()
    core = next(k for k in stats if stats[k]["runs"])
    stamps = stats[core]["runs"]
    starts = [t for k, t in stamps if k == "start"]
    halts = [t for k, t in stamps if k == "halt"]
    rows = []
    for (name, ideal, node), s, h in zip(order, starts, halts, strict=False):
        run = h - s
        rows.append(
            {
                "case": name,
                "run": run,
                "ideal": ideal,
                "rate": round(ideal / run, 3) if run else None,
                "node": node,
            }
        )
        print(
            f"{name:18s} run {run:6d}  ideal {ideal:5d}  rate {ideal / max(run, 1):6.3f}"
            f"  (package {node})"
        )
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
