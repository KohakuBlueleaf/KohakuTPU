"""One combined class/batch/row/column/nk padding witness and its mutant."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels import matmul as M
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    kernel, given = M.quant_kernel(
        Operand("full", pitch=80, offset=16),
        5,
        48,
        16,
        128,
        "b",
        2,
        2,
        2,
        shape_class=ShapeClass(32, 256),
    )
    recs = {}
    for mutant in (None, "l2.packing/pad-nonzero"):
        with rs.mutant(mutant):
            c = K.specialise(kernel, given, machine=machine)
        recs[mutant or "good"] = {"l1": c.l1_text, "name": c.name, "verdict": c.verdict}
        print(mutant, c.verdict)
    kernel, given = M.quant_walk_kernel(
        1760, ((2, 800), (5, 160), (3, 32)), 32, 5, 48, 16, 128, "b", 2, 2, 2
    )
    c = K.specialise(kernel, given, machine=machine)
    recs["walk"] = {"l1": c.l1_text, "name": c.name, "verdict": c.verdict}
    print("walk", c.verdict)
    args.output.write_text(json.dumps(recs))


if __name__ == "__main__":
    main()
