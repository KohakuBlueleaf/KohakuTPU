"""Audit/write machine-only migration of retained mover-QUANT witnesses.

No input, expected value, L1 text, package byte, verdict or binding may change.
The unaffected v9t5 has identical compute/mover facts; current v9t6 must check
these unproven inputs on device. Vector tests witness current-board behavior.
"""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[4]
CASES = (
    "l2-mov-L2.MOV.001.node_ops",
    "l2-mov-L2.MOV.010.quant_and_gt4",
    "dsl-mov-DSL.MOV.003.quant",
    "dsl-graph-DSL.GRAPH.003.node_kinds",
)


def audit(write=False, backup_dir=None):
    machines = {
        n: json.loads(
            (ROOT / f"machines/multimesh_v9t{n}.json").read_text(encoding="utf-8")
        )
        for n in (5, 6)
    }
    old, new = (deepcopy(machines[n]["machine"]) for n in (6, 5))
    for fact in old:
        if fact not in ("conversion", "card"):
            assert old[fact] == new[fact], fact
    for fact in old["card"]:
        if fact not in ("name",):
            assert old["card"][fact] == new["card"][fact], fact
    assert new["conversion"]["fp16_mx7"]["preferred_backend"] == "mover_quant"
    assert old["conversion"]["fp16_mx7"]["allowed_backends"] == ["vector_mx7"]
    paths = [
        ROOT / f"software/rust/golden/{stage}/{case}.json"
        for stage in ("l2", "e2e")
        for case in CASES
    ]
    paths += [
        ROOT / f"software/tests/spec/vectors/l2/mov/{name}.json"
        for name in ("L2.MOV.001.node_ops", "L2.MOV.010.quant_and_gt4")
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        doc = json.loads(text)
        assert doc["machine"] in ("multimesh_v9t6", "multimesh_v9t5")
        updated = text.replace(
            '"machine": "multimesh_v9t6"', '"machine": "multimesh_v9t5"', 1
        )
        expect = deepcopy(doc)
        expect["machine"] = "multimesh_v9t5"
        assert json.loads(updated) == expect
        l2 = doc.get("l2") or "\n".join(doc["input"]["text"]) + "\n"
        checked = _rs.compile(l2, json.dumps(machines[6]), cache_dir="")
        assert "assert_zero" in checked["l1"]
        _rs.compile(l2, json.dumps(machines[5]), cache_dir="")
        print(path.relative_to(ROOT), "machine metadata only")
        if write and text != updated:
            backup = backup_dir / path.relative_to(ROOT)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--backup", type=Path, help="where --write saves the originals")
    args = ap.parse_args()
    if args.write and args.backup is None:
        ap.error("--write needs --backup DIR")
    audit(args.write, args.backup)
