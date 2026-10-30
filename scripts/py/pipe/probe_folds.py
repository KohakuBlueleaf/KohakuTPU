"""Inspect existing relayout plans for every current warm conversion."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels import relayout as RL
from kohakutpu.language import machine as MF


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "capture",
        type=Path,
        help="capture directory with events.json and layouts.json; "
        "plans.json is written beside it",
    )
    args = ap.parse_args()
    base = args.capture
    events = json.loads((base / "events.json").read_text())
    layouts = json.loads((base / "layouts.json").read_text())
    hw = RL.hw_of(MF.load("multimesh_v9t6"), 1)
    records = []
    for e in events:
        if (
            e["kind"] != "convert"
            or e["capture_pass"] != 1
            or e["phase"] != "jit_step2"
        ):
            continue
        ls = [l for l in layouts if l["start"] <= e["index"] < l["stop"]]
        if not ls:
            records.append(
                {"index": e["index"], "key": e["key"], "plan": "not forms.relayout"}
            )
            continue
        l = ls[0]
        try:
            p = RL.plan(
                l["n"],
                tuple(map(tuple, l["walk"])),
                l["at"],
                tuple(map(tuple, l["dwalk"])),
                l["dat"],
                l["m"],
                False,
                hw,
            )
            result = [type(x).__name__ for x in p.parts]
        except RL.Unplanned as exc:
            result = str(exc)
        records.append({"index": e["index"], "key": e["key"], "plan": result})
    (base.parent / "plans.json").write_text(json.dumps(records, indent=2))
    print([(r["index"], r["plan"]) for r in records])


if __name__ == "__main__":
    main()
