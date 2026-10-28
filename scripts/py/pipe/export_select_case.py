"""Inspect the changed L2.OP.006.vsel schedule without updating a golden."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

SOURCE = """target ktpu.v9
fn k.l2(x: f16[8,128] @dram, y: f16[8,128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            xl = load x[t*4 : +4, :] : f16[4,128] @l1
            v = unpack xl : f32[4,128] @reg
            e = exp2 v : f32[4,128] @reg
            m = cmplt v, e : f32[4,128] @reg
            o = sel m, v, e : f32[4,128] @reg
            ol = pack o : f16[4,128] @l1
            store y[t*4 : +4, :] <- ol
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    m = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = {}
    for label, mutant in [("before", "l2.predicate-select/off"), ("after", None)]:
        with rs.mutant(mutant):
            c = K.compile(SOURCE, name="k", machine=m)
        records[label] = dict(
            name=c.name, l1=c.l1_text, verdict=c.verdict, binds={"x": "x", "y": "y"}
        )
    result = dict(
        index="vsel",
        kind="golden-review",
        converts=[],
        consumer_key="L2.OP.006.vsel",
        allocations={"x": 2048, "y": 2048},
        tables={},
        before=[records["before"]],
        after=records["after"],
        output="y",
    )
    args.output.write_text(json.dumps(result))
    print({k: v["verdict"] for k, v in records.items()})


if __name__ == "__main__":
    main()
