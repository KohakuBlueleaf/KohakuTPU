"""Compare native/staged padding on actual regressing card geometries."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from package_stage import stage as package_stage


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    ap.add_argument(
        "--work",
        type=Path,
        required=True,
        help="pipe work tree: reads loop4/ws0, prebuilds into loop6/pair-library",
    )
    args = ap.parse_args()
    library = args.work / "loop6/pair-library"
    qs = json.loads((args.work / "loop4/ws0/quants.json").read_text())
    es = json.loads((args.work / "loop4/ws0/events.json").read_text())
    warm = [
        e
        for e in es
        if e["capture_pass"] == 1 and e["phase"] == "jit_step2" and e["kind"] == "quant"
    ]
    args.output.mkdir(exist_ok=True)
    for e in warm[-2:]:
        q = next(q for q in qs if q["index"] == e["index"])
        params = (
            Operand("full", pitch=q["pitch"], offset=q["offset"]),
            *[q[n] for n in ("rows", "cols", "rp", "cp", "side", "g", "nk", "batches")],
        )
        rec = dict(
            index=q["index"],
            kind="padding",
            converts=[],
            consumer_key=e["key"],
            tables={},
            output="q",
            allocations={},
        )
        for board in ("vlt_v9t6s_1n", "multimesh_v9t6"):
            for strategy in ("native", "staged"):
                k, g = MM.quant_kernel(*params, pad_strategy=strategy)
                c = K.specialise(
                    k, g, machine=machine_of(board, packing_backend="vector_mx7")
                )
                stage = package_stage(c, {p: p for p in g}, library)
                if board == "vlt_v9t6s_1n":
                    rec["before" if strategy == "native" else "after"] = (
                        [stage] if strategy == "native" else stage
                    )
                    rec["allocations"] = {p: s.nbytes() for p, s in g.items()}
                else:
                    rec[strategy + "_card"] = stage
                print(q["index"], board, strategy, c.verdict, flush=True)
        (args.output / f"{q['index']}.json").write_text(json.dumps(rec))


if __name__ == "__main__":
    main()
