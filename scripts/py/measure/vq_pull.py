"""Compile a captured full-size pull class with serial or balanced selection."""

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from vq_census import census


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tree", type=int, default=0)
    args = parser.parse_args()
    bundle = pickle.loads(args.artifact.read_bytes())
    case = next(c for c in bundle["cases"] if c["label"] == "lanes_16eae58723")
    data = {
        name: case["allocations"][v["allocation"]]["data"][
            v["offset"] : v["offset"] + v["nbytes"]
        ]
        for name, v in case["views"].items()
        if v["role"] == "in"
    }
    # Reconstruct the documented captured class, not a production shape case:
    # 1008 VC rows, one gathered source word per output word, 16 rotations.
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    hw = R.hw_of(MF.load(machine.board), 1)
    pull = R.Pull(
        "src",
        "dst",
        64,
        8064,
        1008,
        8064,
        (("list", 0),),
        0,
        tuple(range(16)),
        "zero",
        False,
    )
    pl = R.Plan(
        (R.Fill("dst", 0, 64), pull, R.Fill("dst", 8128, 64)),
        (("po", 1008, 128), ("ps0", 1008, 128), ("pw0", 8064, 16)),
        np.frombuffer(data["index"], dtype="<f4").copy(),
        np.frombuffer(data["mask"], dtype="<f2").reshape(1008, 128).copy(),
        392,
        8192,
    )
    source = ((np.arange(6272) % 997 - 498) / 64).astype("<f2")
    expected = R.execute(
        pl, source.view("<u2"), np.zeros(131072, dtype="<u2"), hw
    ).tobytes()
    if args.tree:
        pl = R.tree_pull(pl, hw)
    kernel, given = R.relayout_kernel(pl, hw)
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
    compiled = rs._rs.compile(
        json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not compiled["verdict"]["refused"], compiled["verdict"]
    work = census(compiled["l1"])
    record = {
        "label": f"pull-tree{args.tree}",
        "kind": "convert",
        "shape": [1008, 128],
        "read_bytes": work["fill_words"] * 32,
        "write_bytes": work["drain_words"] * 32,
        "math_lane_ops": work["lane_slots"],
        "graph": graph,
        "compiled": compiled,
        "inputs": {
            "src": source.tobytes(),
            "index": pl.index.tobytes(),
            "mask": pl.masks.tobytes(),
        },
        "output": "dst",
        "expected": expected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        pickle.dumps({"machine": machine.machine_json, "cases": [record]})
    )
    print(json.dumps({"label": record["label"], "work": work}))


if __name__ == "__main__":
    main()
