"""Compile the final bounded stream and selector-tree parity sessions."""

import argparse
import json
import pickle
import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from vq_census import census
from vq_isolated import compile_artifact
from vq_model import ROOT
from vq_workload import walk

from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("quant", "pull"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = []
    if args.group == "quant":
        original = ROOT / ".plan/vq/loop2/final.pkl"
        bundle = pickle.loads(original.read_bytes())
        for case in bundle["cases"]:
            path = args.output.parent / f"stream-{case['label']}.pkl"
            compile_artifact(
                SimpleNamespace(
                    artifact=original, kernel=case["label"], axes=1, fill=1, output=path
                )
            )
            built = pickle.loads(path.read_bytes())
            work = census(built["compiled"]["l1"])
            ports = {p["name"]: p["spec"] for p in built["graph"]["ports"]}
            records.append(
                dict(
                    label=case["label"],
                    kind="quant",
                    shape=ports["x"]["shape"],
                    read_bytes=work["fill_words"] * 32,
                    write_bytes=work["drain_words"] * 32,
                    math_lane_ops=work["lane_slots"],
                    graph=built["graph"],
                    compiled=built["compiled"],
                    inputs=dict(x=built["source"]),
                    output="q",
                    expected=built["expected"],
                )
            )
        negative = pickle.loads(
            (ROOT / ".plan/vq/loop2/quant-pipeline.pkl").read_bytes()
        )
        case = next(
            c
            for c in negative["cases"]
            if c["label"] == "quant-padded-finite0-batch4-overlap0-pipe1"
        )
        for node in walk(case["graph"]["body"]):
            if node.get("op") == "quant":
                node["kw"] += [
                    ["stream_axes", {"int": 1}],
                    ["fill_overlap", {"int": 1}],
                    ["zero_classifier", {"int": 1}],
                ]
        case["compiled"] = rs._rs.compile(
            json.dumps(case["graph"]),
            rs.machine_json(machine),
            cache_dir="",
            fetch=True,
        )
        assert not case["compiled"]["verdict"]["refused"], case["compiled"]["verdict"]
        args.output.with_name("negative.pkl").write_bytes(
            pickle.dumps(dict(cases=[case]))
        )
    else:
        fixture = runpy.run_path(
            str(ROOT / "software/compiler/tests/test_pull_tree.py")
        )["fixture"]
        for slots, keep, rotations in (
            (1, False, 16),
            (1, True, 8),
            (2, False, 4),
            (2, True, 16),
        ):
            hw, baseline = fixture(slots, keep, rotations)
            source = ((np.arange(slots * 512) % 997 - 498) / 64).astype("<f2")
            previous = np.full(512, 0xD3D3, dtype="<u2")
            expected = R.execute(baseline, source.view("<u2"), previous, hw).tobytes()
            for tree in (0, 1):
                plan = R.tree_pull(baseline, hw) if tree else baseline
                kernel, given = R.relayout_kernel(plan, hw)
                graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
                compiled = rs._rs.compile(
                    json.dumps(graph),
                    rs.machine_json(machine),
                    cache_dir="",
                    fetch=True,
                )
                assert not compiled["verdict"]["refused"], compiled["verdict"]
                work = census(compiled["l1"])
                records.append(
                    dict(
                        label=f"pull-slots{slots}-keep{int(keep)}-tree{tree}",
                        kind="convert",
                        shape=[4, 128],
                        read_bytes=work["fill_words"] * 32,
                        write_bytes=work["drain_words"] * 32,
                        math_lane_ops=work["lane_slots"],
                        graph=graph,
                        compiled=compiled,
                        inputs=dict(
                            src=source.tobytes(),
                            index=plan.index.tobytes(),
                            mask=plan.masks.tobytes(),
                        ),
                        output="dst",
                        expected=expected,
                    )
                )
    args.output.write_bytes(
        pickle.dumps(dict(machine=machine.machine_json, cases=records))
    )
    print("compiled", len(records), args.group)


if __name__ == "__main__":
    main()
