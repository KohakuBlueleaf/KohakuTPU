"""Offset/stride/broadcast direct-row witnesses, staged and direct, no model opened."""

import json
import os
import pickle
from pathlib import Path

import numpy as np
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from vq_census import census

ROOT = Path(__file__).resolve().parents[3]


def main():
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    cases = []
    for direct in (0, 1):
        os.environ["KTPU_CLASS_ROWS_DIRECT"] = str(direct)
        for rows in (16, 48):
            width, pitch, offset = 128, 160, 16
            source = np.full((rows, pitch), np.nan, dtype="<f2")
            values = (
                ((np.arange(rows * width) % 16 - 8) / 16)
                .astype("<f2")
                .reshape(rows, width)
            )
            source[:, offset : offset + width] = values
            for kind in ("sum", "max", "col", "scalar"):
                operands = [V.Operand("full", pitch=pitch, offset=offset)]
                inputs = {"in0": source.tobytes()}
                if kind in ("sum", "max"):
                    expr = X.Reduce(kind, X.Load(0))
                    out = V.Operand("row")
                    expected = (
                        values.astype(np.float32).sum(-1)
                        if kind == "sum"
                        else values.max(-1)
                    ).astype("<f2")
                else:
                    expr = X.op("add", X.Load(0), X.Load(1))
                    if kind == "col":
                        bias = np.full(16 + width, np.nan, dtype="<f2")
                        bias[16:] = (np.arange(width) % 8) / 8
                        operands.append(V.Operand("col", offset=16))
                        value = values + bias[16:]
                    else:
                        bias = np.full(16, np.nan, dtype="<f2")
                        bias[5] = 0.25
                        operands.append(V.Operand("scalar", offset=5))
                        value = values + bias[5]
                    inputs["in1"] = bias.tobytes()
                    out = V.Operand("full", pitch=pitch, offset=offset)
                    expected = np.full((rows, pitch), 0xD3D3, dtype="<u2").view("<f2")
                    expected[:, offset : offset + width] = value
                kernel, given, _ = V.rows_kernel(
                    expr,
                    tuple(operands),
                    rows,
                    width,
                    out,
                    1,
                    shape_class=ShapeClass(32, 256),
                )
                assert kernel.class_info["direct"] == bool(direct)
                graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
                compiled = rs._rs.compile(
                    json.dumps(graph),
                    rs.machine_json(machine),
                    cache_dir="",
                    fetch=True,
                )
                assert not compiled["verdict"]["refused"], compiled["verdict"]
                work = census(compiled["l1"])
                cases.append(
                    {
                        "read_bytes": work["fill_words"] * 32,
                        "write_bytes": work["drain_words"] * 32,
                        "math_lane_ops": work["lane_slots"],
                        "label": f"offset-{rows}-{kind}-direct{direct}",
                        "kind": "dsl",
                        "shape": [rows, width],
                        "graph": graph,
                        "compiled": compiled,
                        "inputs": inputs,
                        "output": "out",
                        "expected": expected.tobytes(),
                    }
                )
    (ROOT / ".plan/vq/loop7/direct-witness.pkl").write_bytes(
        pickle.dumps({"machine": machine.machine_json, "cases": cases})
    )
    print("compiled", len(cases), "offset witnesses")


if __name__ == "__main__":
    main()
