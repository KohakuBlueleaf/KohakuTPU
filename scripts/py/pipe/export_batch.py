"""Lower native PIPE packing cases on the build host for desktop model execution."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def source(side, batch, rows=224, cols=32, groups=56):
    return (
        "target ktpu.v9\n"
        f"fn q.l2(x: f16[{rows},{cols}] @dram, q: mx7{side}[{rows},{cols}] "
        f"@dram(g{'m' if side == 'a' else 'n'}={groups},nk=1))\n"
        f"    quant q <- x finite_batch={batch}\n"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = {}
    for side in ("a", "b"):
        for batch in (1, 2, 4):
            for mutant in (
                None,
                "l2.packing/batch-no-pack-wait",
            ):
                if mutant and batch != 4:
                    continue
                with rs.mutant(mutant):
                    c = K.compile(source(side, batch), name="q", machine=machine)
                key = f"{side}-{batch}-{mutant or 'good'}"
                records[key] = {"l1": c.l1_text, "verdict": c.verdict}
                print(key, c.verdict["refused"], len(c.verdict["findings"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, indent=1))


if __name__ == "__main__":
    main()
