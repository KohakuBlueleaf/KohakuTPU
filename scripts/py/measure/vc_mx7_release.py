"""Supplement the issue-gap calibration with MX7 source read-release rings."""

import argparse
import json
from pathlib import Path

from measure import vc_timing as timing

from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[3]
BASE_PROGRAM = timing.program


def program(kind, k, vl, dependent, regs=128, copies=2):
    return BASE_PROGRAM("pack_ring", k, vl, dependent, regs, copies).replace(
        f"f16 n={vl // 16}", kind
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output", type=Path, required=True, help="probe series JSON to write"
    )
    args = ap.parse_args()
    record = json.loads((ROOT / "machines/measured/vc_timing.v21.mq1.json").read_text())
    machine = (ROOT / "machines/multimesh_v9t6.json").read_text()
    build = ROOT / "build/r63-v9t6"
    timing.build_model(build, record["defines"])
    timing.program = program
    probes = [
        timing.measure_series(kind, 128, dep, machine, build, record["defines"])
        for kind in ("mx7a", "mx7b")
        for dep in (True, False)
    ]
    for p in probes:
        if p["dependent"]:
            model_cycles = [
                json.loads(
                    _rs.spec_run(
                        "l2.timing",
                        json.dumps(
                            {"text": r["program"].splitlines(), "name": "bench"}
                        ),
                        machine="multimesh_v9t6",
                    )
                )
                for r in p["runs"]
            ]
            p["reader_fit"] = timing.fit(list(timing.KS), model_cycles)
            assert p["reader_fit"]["slope"] == p["fit"]["slope"] == 45
            assert not any(p["fit"]["residuals"])
        print(p["kind"], p["dependent"], p["fit"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(probes, indent=2) + "\n")


if __name__ == "__main__":
    main()
