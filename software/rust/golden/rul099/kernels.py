"""Audit/write RUL.099's ruled `matmul.plan` results in golden/kernels.

    python software/rust/golden/rul099/kernels.py [--write --backup DIR]

A recorded plan on a board whose packing policy prefers mover QUANT pairs K
blocks only under the `vector` pairing; under `unpaired` (the stack's
`MX7_PAIRING`) it plans nk = 1. Each recorded plan the ruling changes gets
`ruled = {"rulings": ["RUL.099"], "result": <the recording, nk = 1>}`; nothing
else in the file changes. The rule is restated here from the board's machine
file, not read from the Rust plan.
"""

import argparse
import json
import shutil
from copy import deepcopy
from pathlib import Path

from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[4]
KERNELS = ROOT / "software/rust/golden/kernels"


def preferred(board):
    doc = json.loads((ROOT / f"machines/{board}.json").read_text(encoding="utf-8"))
    return doc["machine"]["conversion"]["fp16_mx7"]["preferred_backend"]


def ruled(call, pairing):
    """The RUL.099 result of a recorded `matmul.plan` call, or None when the
    ruling leaves it."""
    result = call.get("result")
    if call["fn"] != "matmul.plan" or not result or result["mm"]["nk"] < 2:
        return None
    if pairing == "vector" or preferred(call["machine"]["board"]) != "mover_quant":
        return None
    out = deepcopy(result)
    out["mm"]["nk"] = 1
    return {"rulings": ["RUL.099"], "result": out}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--backup", type=Path)
    args = ap.parse_args()
    if args.write and not args.backup:
        raise SystemExit("--write needs --backup DIR")
    pairing = _rs.MX7_PAIRING
    changed = {}
    for path in sorted(KERNELS.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        doc = json.loads(text)
        calls = [c for c in doc["calls"] if "ruled" not in c and ruled(c, pairing)]
        if not calls:
            continue
        for c in calls:
            c["ruled"] = ruled(c, pairing)
        changed[path.name] = len(calls)
        if args.write:
            args.backup.mkdir(parents=True, exist_ok=True)
            target = args.backup / path.name
            if target.exists():
                raise SystemExit(f"{target} exists")
            shutil.copy2(path, target)
            line = text.splitlines()[1]
            indent = len(line) - len(line.lstrip(" "))
            path.write_text(json.dumps(doc, indent=indent) + "\n", encoding="utf-8")
    print(json.dumps({"pairing": pairing, "files": changed}, indent=2))
    print(sum(changed.values()), "calls in", len(changed), "files")


if __name__ == "__main__":
    main()
