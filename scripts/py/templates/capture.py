"""Capture one batch-8 LeNet/Adam16 step, without compiling or executing kernels."""

import argparse
import dataclasses
import json
import os
import pathlib
import runpy
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from kohakutpu.application.kernels import (
    convert,
    expr,
    lanes,
    matmul,
    transpose,
    vector,
)
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.plan.emit import parse_expr
from kohakutpu.application.runtime import device, forms, generated
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters
from tinygrad.tensor import Tensor

ROOT = pathlib.Path(__file__).resolve().parents[3]
GENERATORS = (
    (vector, "rows_kernel", "rows"),
    (vector, "flat_kernel", "flat"),
    (matmul, "mm_kernel", "mm"),
    (matmul, "quant_kernel", "quant"),
    (convert, "walk_kernel", "walk"),
    (convert, "fill_kernel", "fill"),
    (transpose, "transpose_kernel", "transpose"),
    (transpose, "reverse_kernel", "reverse"),
    (convert, "copy_kernel", "copy"),
    (lanes, "relayout_kernel", "lanes"),
)
#: A Rust generator's family above; a launch of another keeps its own name.
FAMILIES = {
    "vector.rows_kernel": "rows",
    "vector.flat_kernel": "flat",
    "matmul.mm_kernel": "mm",
    "matmul.quant_kernel": "quant",
    "convert.walk_kernel": "walk",
    "convert.fill_kernel": "fill",
    "transpose.transpose_kernel": "transpose",
    "transpose.reverse_kernel": "reverse",
    "convert.copy_kernel": "copy",
    "lanes.relayout_kernel": "lanes",
}


def python_arguments(generator, a, machine) -> dict:
    """The encoded arguments of `FAMILIES`' Python factory for a generator
    call `a` (what `derive` reads and traces); a call of no family, as it is."""
    cores, clusters = machine.count("VC"), machine.count("MG")
    if generator == "vector.rows_kernel":
        kw = {
            "e": parse_expr(a["e"]),
            "operands": tuple(vector.Operand(**o) for o in a["operands"]),
            "R": a["R"],
            "W": a["W"],
            "out": vector.Operand(**a["out"]),
            "cores": cores,
            "bands": a.get("bands"),
        }
    elif generator == "vector.flat_kernel":
        kw = {
            "e": parse_expr(a["e"]),
            "dtypes": tuple(a["dtypes"]),
            "n": a["n"],
            "out_dtype": a["out_dtype"],
            "cores": cores,
            "scalars": tuple(map(tuple, a["scalars"])),
        }
    elif generator == "matmul.mm_kernel":
        plan = json.dumps(a["mm"]["made_by"]["args"])
        kw = {
            "mm": matmul.Mm(
                **json.loads(rs._rs.mm_plan(plan, rs.machine_json(machine)))
            ),
            "e": parse_expr(a["e"]),
            "extras": tuple(vector.Operand(**o) for o in a["extras"]),
            "out": vector.Operand(**a["out"]),
            "clusters": clusters,
            "cores": cores,
        }
    elif generator == "matmul.quant_kernel":
        names = "rows", "cols", "rp", "cp", "side", "g", "nk", "batches"
        kw = dict(src=vector.Operand(**a["src"]), **{n: a[n] for n in names})
    elif generator in ("transpose.transpose_kernel", "transpose.reverse_kernel"):
        kw = dict(a, cores=cores)
    else:
        kw = a
    return encode(kw)


def encode(value, memo=None):
    memo = {} if memo is None else memo
    reference = None
    if isinstance(value, (expr.Load, expr.Const, expr.Op, expr.Reduce)):
        if id(value) in memo:
            return {"ref": memo[id(value)]}
        reference = len(memo)
        memo[id(value)] = reference
    if isinstance(value, np.ndarray):
        return {"type": "ndarray", "dtype": str(value.dtype), "values": value.tolist()}
    if isinstance(value, np.generic):
        return value.item()
    if dataclasses.is_dataclass(value):
        return {
            "type": type(value).__name__,
            **({"id": reference} if reference is not None else {}),
            "fields": {
                f.name: encode(getattr(value, f.name), memo)
                for f in dataclasses.fields(value)
            },
        }
    if isinstance(value, (tuple, list)):
        return [encode(v, memo) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v, memo) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported generator argument {type(value)}")


def program():
    """The example's module, on the offline board."""
    os.environ["KOHAKUTPU_DEVICE"] = "offline"
    os.environ["KOHAKUTPU_BOARD"] = "multimesh_v9t6"
    return runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py")
    )


def step(example, model="LeNet"):
    """One batch-8 `model`/Adam16 step of `example` on the attached device."""
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = example[model]()
    place(model)
    opt = Adam16(get_parameters(model), lr=2e-3)
    x = np.zeros((8, 784), dtype=np.float16)
    y = np.arange(8, dtype=np.int32)
    example["train"](model, opt, x, y, 1, 8, "KTPU", 1, False)


def capture(path, model="LeNet", library=None):
    """One step's launches, from the hints of `library` a run of it starts from."""
    if path.exists():
        raise FileExistsError(f"reuse the saved capture: {path}")
    example = program()
    rt = device.attach(device.Device(kind="offline", board="multimesh_v9t6"))
    forms.seed_hints(rt, library)
    records, launches, calls, ids = [], [], {}, {}
    graph = generated.graph

    def made(generator, arguments, machine, mutant):
        source = graph(generator, arguments, machine, mutant)
        calls[source] = (generator, json.loads(arguments))
        return source

    def record(source, name, family, arguments, params=()):
        if source not in ids:
            ids[source] = len(records)
            records.append(
                {
                    "id": ids[source],
                    "name": name,
                    "generator": family,
                    "arguments": arguments,
                    "graph": json.loads(source),
                }
            )
        return SimpleNamespace(name=name, params=params, capture_id=ids[source])

    def kernel(self, source, graph=None, traced=None):
        name, (generator, arguments) = json.loads(source)["name"], calls[source]
        return record(
            source,
            name,
            FAMILIES.get(generator, generator),
            python_arguments(generator, arguments, self.machine),
            json.loads(source)["ports"],
        )

    def specialise(self, kernel_, given, **consts):
        traced = kernel_.trace(given, machine=K.dsl_machine(self.machine), **consts)
        source = json.dumps(traced.graph)
        return record(source, kernel_.name, "dsl", {}, traced.params)

    def run(self, compiled, binds, kind="dsl", info=None):
        if self.kind != "offline":
            raise RuntimeError("capture requires an offline device")
        launches.append({"record": compiled.capture_id, "kind": kind})
        result = device.Run(compiled.name, 0, 0.0, kind, dict(info or {}))
        self.runs.append(result)
        return result

    def no_compile(*args, **kwargs):
        raise AssertionError("a capture must never compile")

    with ExitStack() as stack:
        stack.enter_context(patch.object(generated, "graph", made))
        stack.enter_context(patch.object(device.Device, "kernel", kernel))
        stack.enter_context(patch.object(device.Device, "specialise", specialise))
        stack.enter_context(patch.object(rs._rs, "compile", no_compile))
        stack.enter_context(patch.object(rs._rs, "compile_many", no_compile))
        stack.enter_context(patch.object(device.Device, "run", run))
        step(example, model)
    doc = {
        "board": "multimesh_v9t6",
        "batch": 8,
        "input": "synthetic zeros, same training schedule and shapes as the example",
        "machine_json": rs.machine_json(rt.machine),
        "records": records,
        "launches": launches,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    print(
        json.dumps(
            Counter(records[x["record"]]["generator"] for x in launches), sort_keys=True
        )
    )
    print(f"saved {len(launches)} launches, {len(records)} specialisations to {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=pathlib.Path)
    capture(ap.parse_args().output)


if __name__ == "__main__":
    main()
