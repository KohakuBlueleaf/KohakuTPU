"""Per-plan checks of the synchronous training path against CPU arithmetic."""

import importlib.util
import json
from pathlib import Path

import numpy as np
from tinygrad import Tensor, nn
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

from kohakutpu.application.ktpugrad import fold as F
from kohakutpu.application.ktpugrad import reference, run
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated


def synchronous_base(dev, monkeypatch):
    read, write = D.Device.read, D.Device.write

    def read_all(self, *args):
        self.drain()
        return read(self, *args)

    def write_all(self, *args):
        self.drain()
        return write(self, *args)

    monkeypatch.setattr(D.Device, "read", read_all)
    monkeypatch.setattr(D.Device, "write", write_all)
    dev.queued = False
    dev.max_inflight = 2


def test_original_with_base_waits(dev, monkeypatch):
    synchronous_base(dev, monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "training_witness", Path(__file__).with_name("test_train.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_a_module_trains_one_step(dev)


def test_first_wrong_training_plan(dev, monkeypatch, tmp_path):
    synchronous_base(dev, monkeypatch)
    # A fused group's plans run when its last one does: nothing to read per plan.
    monkeypatch.setenv("KTPU_HORIZONTAL_FUSION", "0")
    original = run.execute
    checked = []
    generated_calls = []
    launch = generated.launch

    def observe(rt, generator, arguments, bindings, *args, **kwargs):
        inputs = {}
        for key, address in bindings.items():
            if key.startswith("in"):
                entry, physical = rt.mmu.resolve(address)
                size = min(64, entry.extent - (physical - entry.physical))
                inputs[key] = np.frombuffer(rt.read(address, size), np.float16).tolist()
        generated_calls.append(
            {
                "generator": generator,
                "arguments": arguments,
                "bindings": bindings,
                "input_f16_prefix": inputs,
            }
        )
        return launch(rt, generator, arguments, bindings, *args, **kwargs)

    monkeypatch.setattr(generated, "launch", observe)

    def check(rt, plan, buffers):
        arrays = {}
        for access in [*plan.accesses, plan.a, plan.b]:
            if access is None or access.slot is None or access.slot in arrays:
                continue
            view = buffers[access.slot]
            dtype = np.dtype(F.np_dtype(access.dtype))
            arrays[access.slot] = np.frombuffer(
                FM.read(rt, view, dtype, view.nbytes // dtype.itemsize), dtype
            ).copy()
        expected = (
            np.asarray(plan.value)
            if plan.kind == "const"
            else reference.evaluate(plan, arrays)
        ).astype(np.float32)
        runs = original(rt, plan, buffers)
        rt.drain()
        view = buffers[plan.out_slot]
        dtype = np.dtype(F.np_dtype(plan.out_dtype))
        got = np.frombuffer(FM.read(rt, view, dtype, plan.out_n), dtype).reshape(
            plan.out_shape
        )
        error = float(np.max(np.abs(got.astype(np.float32) - expected)))
        scale = max(float(np.max(np.abs(expected))), 1e-9)
        checked.append(
            {
                "index": len(checked),
                "name": plan.name,
                "kind": plan.kind,
                "runs": [r.name for r in runs],
                "relative_error": error / scale,
            }
        )
        # MXFP7 product quantisation is distinct from vector-lane arithmetic.
        bound = 0.2 if plan.kind == "matmul" else 0.02
        if not np.isfinite(error) or error / scale > bound:
            evidence = {
                "checked": checked,
                "plan": repr(plan),
                "inputs": {str(k): a.tolist() for k, a in arrays.items()},
                "output": got.tolist(),
                "expected_cpu_f32": expected.tolist(),
                "compiled": [r.info["compiled"].l1.source for r in runs],
                "l2": [r.info["compiled"].l2_text for r in runs],
                "generated_calls": generated_calls,
            }
            path = tmp_path / "first-wrong-kernel.json"
            path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            raise AssertionError(f"first wrong plan: {checked[-1]}; evidence {path}")
        return runs

    monkeypatch.setattr(run, "execute", check)
    rng = np.random.default_rng(3)
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = nn.Linear(64, 32)
    place(model)
    x = Tensor(rng.standard_normal((32, 64)).astype(np.float16), device="KTPU")
    y = Tensor(rng.integers(0, 32, 32).astype(np.int32), device="KTPU")
    opt = Adam16(get_parameters(model), lr=1e-2)
    with Context(TRAINING=1):
        loss = model(x).sparse_categorical_crossentropy(y)
        opt.zero_grad()
        opt.scale(loss).backward()
        Tensor.realize(loss, *opt.schedule_step())
    loss.item()
