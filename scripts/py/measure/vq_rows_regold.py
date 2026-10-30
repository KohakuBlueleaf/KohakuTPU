"""Export a planner output-column rebasing witness using the Python normalizer."""

import argparse
import json
import shutil
from pathlib import Path

from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import rows
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    original = ROOT / "software/rust/golden/plan" / args.fixture
    source = args.source if args.source is not None else original
    doc = json.loads(source.read_text())
    machine = machine_of(doc["machine"], nodes=doc["nodes"])
    changes = 0
    for run in doc["runs"]:
        for step in run["steps"]:
            launch = step.get("launch")
            if launch is None or launch["call"]["gen"] != "vector.rows_kernel":
                continue
            call = launch["call"]
            out = Operand(**call["args"]["out"])
            bound = launch["binds"]["out"]
            if out.kind != "full" or not (bound["offset"] // 2) % out.pitch:
                continue
            # Feed the original allocation-relative address to the same Python
            # normalizer used after _place. Compare every touched element.
            relative = out.offset + bound["offset"] // 2
            new = Operand(**dict(vars(out), offset=relative))
            (new,), binding = rows._based([new], {"out": 0}, "out")
            assert binding["out"] + 2 * new.offset == 2 * relative
            call["args"]["out"] = vars(new)
            bound["offset"] = binding["out"]
            graph = json.loads(
                rs._rs.generate(
                    call["gen"],
                    json.dumps(call["args"]),
                    rs.machine_json(machine),
                    None,
                )
            )
            launch["name"] = graph["name"]
            launch["graph"] = graph
            changes += 1
    assert changes
    for directory in ("plan", "originals"):
        (args.output / directory).mkdir(parents=True, exist_ok=True)
    with (args.output / "originals" / args.fixture).open("xb") as stream:
        stream.write(source.read_bytes())
    dest = args.output / "plan" / args.fixture
    with dest.open("x") as stream:
        json.dump(doc, stream, indent=2)
        stream.write("\n")
    shutil.copyfile(dest, original)
    print(json.dumps({"file": "plan/" + args.fixture, "launches": changes}))


if __name__ == "__main__":
    main()
