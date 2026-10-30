"""Native padding and batching variants, lowered on the host for model witnesses."""

import argparse
import json
import os
from pathlib import Path

from kohakutpu.application.kernels import matmul
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = {}
    for rows, cols, groups in ((224, 48, 56), (6272, 400, 56)):
        for side in (("a", "b") if rows == 224 else ("a",)):
            for pad, batch in ((0, 1), (0, 4), (1, 1), (1, 4)):
                for mutant in (
                    (None, "l2.packing/pad-nonzero")
                    if pad and rows == 224 and batch == 4
                    else (None,)
                ):
                    os.environ["KTPU_PIPE_FINITE_BATCH"] = str(batch)
                    os.environ["KTPU_PIPE_PAD"] = str(pad)
                    kernel, given = matmul.quant_kernel(
                        Operand("full", pitch=cols, rows=rows),
                        rows,
                        cols,
                        rows,
                        cols + 16,
                        side,
                        groups,
                        1,
                    )
                    with rs.mutant(mutant):
                        c = K.specialise(kernel, given, machine=machine)
                    key = f"{rows}-{side}-{pad}-{batch}-{mutant or 'good'}"
                    records[key] = {
                        "l1": c.l1_text,
                        "verdict": c.verdict,
                        "name": c.name,
                        "rows": rows,
                        "cols": cols,
                        "groups": groups,
                    }
                    print(key, c.verdict["refused"], len(c.verdict["findings"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, indent=1))


if __name__ == "__main__":
    main()
