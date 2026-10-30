"""Comparison/select lowering on native host: baseline, fused and wrong arm."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

SOURCE = """target ktpu.v9
fn select.l2(x: f16[16,128] @dram, selector: f16[16,128] @dram, y: f16[16,128] @dram)
    par v in 0..1 on vc[v]
        for i in 0..16
            a = load x[i : +1, :] : f16[1,128] @l1
            b = load selector[i : +1, :] : f16[1,128] @l1
            av = unpack a : f32[1,128] @reg
            bv = unpack b : f32[1,128] @reg
            m = cmpeq bv, 1.0 : f32[1,128] @reg
            z = sel m, av, -0.0 : f32[1,128] @reg
            q = pack z : f16[1,128] @l1
            store y[i : +1, :] <- q
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    recs = {}
    for label, mutant in (
        ("before", "l2.predicate-select/off"),
        ("after", None),
        ("mutant", "l2.predicate-select/wrong-arm"),
    ):
        with rs.mutant(mutant):
            c = K.compile(SOURCE, name="select", machine=machine)
        recs[label] = {"l1": c.l1_text, "verdict": c.verdict}
        print(label, c.verdict)
    args.output.write_text(json.dumps(recs))


if __name__ == "__main__":
    main()
