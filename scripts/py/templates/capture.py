"""Capture one batch-8 LeNet/Adam16 step, without compiling or executing kernels."""

import argparse
import dataclasses
import functools
import inspect
import json
import os
import pathlib
import runpy
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters
from tinygrad.tensor import Tensor

from kohakutpu.application.kernels import (
    convert,
    expr,
    lanes,
    matmul,
    transpose,
    vector,
)
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

ROOT = pathlib.Path(__file__).resolve().parents[3]
GENERATORS = (
    (vector, "rows_kernel", "rows"),
    (vector, "flat_kernel", "flat"),
    (matmul, "mm_kernel", "mm"),
    (matmul, "quant_kernel", "quant"),
    (convert, "walk_kernel", "walk"),
    (convert, "fill_kernel", "fill"),
    (convert, "cast_kernel", "cast"),
    (transpose, "transpose_kernel", "transpose"),
    (transpose, "reverse_kernel", "reverse"),
    (convert, "copy_kernel", "copy"),
    (lanes, "relayout_kernel", "lanes"),
)


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


def capture(path):
    if path.exists():
        raise FileExistsError(f"reuse the saved capture: {path}")
    os.environ["KOHAKUTPU_DEVICE"] = "offline"
    os.environ["KOHAKUTPU_BOARD"] = "multimesh_v9t6"
    example = runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py")
    )
    rt = device.attach(device.Device(kind="offline", board="multimesh_v9t6"))
    records, launches, generated = [], [], {}

    def wrapped(fn, family):
        @functools.wraps(fn)
        def call(*args, **kwargs):
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            result = fn(*args, **kwargs)
            generated[id(result[0])] = (result[0], family, encode(bound.arguments))
            return result

        return call

    def specialise(kernel, given, machine=None, **consts):
        _, family, arguments = generated[id(kernel)]
        traced = kernel.trace(given, machine=K.dsl_machine(machine), **consts)
        record = {
            "id": len(records),
            "name": kernel.name,
            "generator": family,
            "arguments": arguments,
            "graph": traced.graph,
        }
        records.append(record)
        return SimpleNamespace(
            name=kernel.name, params=traced.params, capture_id=record["id"]
        )

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
        for module, name, family in GENERATORS:
            stack.enter_context(
                patch.object(module, name, wrapped(getattr(module, name), family))
            )
        stack.enter_context(patch.object(K, "specialise", specialise))
        stack.enter_context(patch.object(K, "compile", no_compile))
        stack.enter_context(patch.object(K, "compile_traced", no_compile))
        stack.enter_context(patch.object(device.Device, "run", run))
        Tensor.manual_seed(0)
        with Context(DEV="CPU"):
            model = example["LeNet"]()
        place(model)
        opt = Adam16(get_parameters(model), lr=2e-3)
        x = np.zeros((8, 784), dtype=np.float16)
        y = np.arange(8, dtype=np.int32)
        example["train"](model, opt, x, y, 1, 8, "KTPU", 1, False)
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
