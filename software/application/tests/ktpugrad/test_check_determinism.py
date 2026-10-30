"""Repeat witnesses use fake executable kernels, never a behavior-model oracle."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from tinygrad import Tensor
from tinygrad.engine.jit import TinyJit

from kohakuaccel.driver.node.queue.client import Completion, NodeError
from kohakutpu.application.checks.determinism import (
    DeterminismChecker,
    RepeatComplete,
    StorageSnapshot,
    byte_difference,
)
from kohakutpu.application.checks.kernels import CPUKernel, f16_storage
from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView


def test_cli_records_nonfinite_loss_without_losing_json():
    cli = runpy.run_path(str(Path(D.ROOT) / "scripts/py/check/step_kernels.py"))
    result = dict(losses=[], passed=True)
    for step, value in enumerate((1.0, np.nan, np.inf, -np.inf, None)):
        cli["record_loss"](result, step, value)
    serialized = json.dumps(result, allow_nan=False)
    assert json.loads(serialized)["losses"] == [1.0, None, None, None, None]
    assert [r["step"] for r in result["nonfinite_losses"]] == [1, 2, 3]
    assert result["passed"]  # bitwise determinism is not finite-output accuracy


def test_bit_comparison_signed_zero_nan_payload_and_bytes():
    a = np.array([0, 0x7E01, 0x3C00], dtype="<u2").tobytes()
    b = np.array([0x8000, 0x7E02, 0x3C00], dtype="<u2").tobytes()
    diff = byte_difference(a, b, "<f2")
    assert diff["differing_elements"] == 2
    assert [r["index"] for r in diff["first_differences"]] == [0, 1]
    assert byte_difference(a, a, "<f2")["identical"]
    assert byte_difference(a, b)["differing_elements"] == 2


@pytest.mark.parametrize("jit", [False, True])
@pytest.mark.parametrize("nondeterministic", [False, True])
def test_schedule_eager_jit_detects_fake_variance(monkeypatch, jit, nondeterministic):
    dev = D.Device("offline", board="multimesh_v9t6")
    dev.kind = "cpu_fixture"
    calls = 0
    snapshots = []
    checker = None

    def execute(device, plan, buffers):
        nonlocal calls
        ast = checker.asts.get(id(plan), getattr(plan, "_check_original_ast", None))
        cpu = CPUKernel(ast)
        arrays = {
            slot: np.frombuffer(
                device.read(buffers[slot].addr, buffers[slot].nbytes), np.float16
            ).astype(np.float32)
            for slot in cpu.params
        }
        values = cpu.execute(cpu.ast, arrays)
        if checker.active:
            snapshots.append(arrays[1].copy())
            calls += 1
        for slot, value in values.items():
            if checker.active and nondeterministic and calls == 2:
                value[0] += 1
            device.write(buffers[slot].addr, f16_storage(value).tobytes())
            buffers[slot].wrote(FLAT)
        return []

    monkeypatch.setattr(KD.R, "execute", execute)
    try:
        with D.attached(dev), DeterminismChecker(dev, 3) as checker:

            def step(x):
                return ((x + 1).realize() + 1).realize()

            fn = TinyJit(step) if jit else step
            for i in range(3 if jit else 1):
                checker.active = i == (2 if jit else 0)
                y = fn(Tensor(np.array([1, 2, 3, 4], np.float16), device="KTPU"))
            assert len(snapshots) == 6
            for i, snapshot in enumerate(snapshots):
                np.testing.assert_array_equal(snapshot, np.arange(1, 5) + i // 3)
            np.testing.assert_array_equal(y.numpy(), [3, 4, 5, 6])
        report = checker.report()
        assert report["passed"] is not nondeterministic
        assert report["graph_replays"] == int(jit)
        row = report["rows"][0]
        assert row["distinct_outputs"] == (2 if nondeterministic else 1)
        if nondeterministic:
            assert (
                row["comparisons"][0]["outputs"][0]["first_differences"][0]["index"]
                == 0
            )
    finally:
        dev.close()


@pytest.mark.parametrize("mode", ["deterministic", "variable", "stall", "fault"])
def test_package_loop_inplace_alias_restored_and_stops(monkeypatch, mode):
    dev = D.Device("offline", board="multimesh_v9t6")
    dev.kind = "cpu_fixture"
    at = dev.alloc(64)
    dev.write(at, np.arange(32, dtype=np.float16).tobytes())
    initial = dev.read(at, 64)
    borrowed = dev.alloc(64)
    dev.write(borrowed, bytes(64))
    spec = "f16[4] @dram"
    compiled = SimpleNamespace(
        name="test_package",
        l1=SimpleNamespace(params=lambda *args: (("x", spec), ("out", spec))),
        machine=dev.machine,
        scratch=[dict(space="dram", bytes=64, members=[])],
    )
    inputs = []

    def fake(device, compiled, bindings, *, kind="dsl", info=None):
        inputs.append(device.read(at, 64))
        if mode in ("stall", "fault") and len(inputs) == 3:
            raise NodeError(
                Completion(1, 0x30 if mode == "stall" else 0x20, 0, 0, 0, 0)
            )
        # A bound arena is where the run's scratch is, restored each repeat.
        scratch_at = bindings["scratch:dram"]
        assert scratch_at == borrowed and device.read(scratch_at, 64) == bytes(64)
        device.write(scratch_at, bytes([7]) * 64)
        raw = bytearray(inputs[-1])
        raw[0] = 10
        raw[8] = 17 + (len(inputs) % 2 if mode == "variable" else 0)
        device.write(at, raw)
        result = D.Run(compiled.name, 1, 0.0, kind, {})
        device.runs.append(result)
        return SimpleNamespace(run=result)

    monkeypatch.setattr(D.Device, "submit", fake)
    try:
        with DeterminismChecker(dev, 5, package_prefix="test_") as checker:
            checker.observe_packages = True
            checker.current = 12
            with pytest.raises(
                NodeError if mode in ("stall", "fault") else RepeatComplete
            ):
                dev.submit(
                    compiled,
                    {"x": at, "out": at + 8, "scratch:dram": borrowed},
                    kind="quant",
                )
        row = checker.packages[0]
        assert dev.mmu.resolve(borrowed)[0].live
        assert all(raw == initial for raw in inputs)
        assert row["attempts"] == (3 if mode in ("stall", "fault") else 5)
        assert row["stalls"] == int(mode == "stall")
        assert row["node_errors"] == int(mode in ("stall", "fault"))
        assert row["distinct_outputs"] == (2 if mode == "variable" else 1)
        assert checker.report()["passed"] == (mode == "deterministic")
        if mode == "variable":
            assert row["comparisons"][0]["outputs"][0]["output"] == "out"
            assert row["comparisons"][0]["outputs"][0]["differing_elements"] == 1
        if mode == "deterministic":
            assert dev.read(at, 1) == bytes([10])
    finally:
        dev.close()


def test_snapshot_new_grown_forms_and_nonzero_allocation_tail():
    dev = D.Device("offline", board="multimesh_v9t6")
    try:
        st = Storage(dev, 6)
        entry, _ = dev.mmu.resolve(st.addr)
        raw = bytes([91]) * entry.extent
        dev.write(st.addr, raw)
        key = ("grown",)
        st.form(key, 32)
        snapshot = StorageSnapshot(dev, {0: st})
        with DeterminismChecker(dev, 2) as checker:
            checker.initialize_allocations = True
            grown = st.form(key, 1024)
            new = st.form(("new",), 32)
            dev.write(grown.addr, bytes([10]) * 1024)
            dev.write(new.addr, bytes([11]) * 32)
            dev.write(st.addr, bytes(6))
            snapshot.restore()
            assert ("new",) not in st.forms and st.forms[key].nbytes == 32
            assert dev.read(st.addr, entry.extent) == raw
            new = st.form(("new",), 32)
            assert dev.read(new.addr, 32) == bytes(32)
    finally:
        dev.close()


def test_real_submission_scratch_metadata_and_restoration(monkeypatch):
    dev = D.Device("offline", board="vlt_v9t6s_1n")
    dev.kind = "lifecycle_fixture"  # exercises bookkeeping, NOT numerical validation
    observed = []
    original = D.Device.submit

    def audit(device, compiled, binds, kind="dsl", info=None):
        if checker.observe_packages:
            observed.append((compiled.name, list(compiled.scratch or ()), dict(binds)))
        return original(device, compiled, binds, kind, info)

    monkeypatch.setattr(D.Device, "submit", audit)
    try:
        with D.attached(dev), DeterminismChecker(dev, 2) as checker:
            checker.active = True
            a = Tensor(np.ones((32, 32), np.float16), device="KTPU").realize()
            (a @ a).realize()
        assert checker.packages and all(r["completed"] == 2 for r in checker.packages)
        scratch_calls = [r for r in observed if r[1]]
        assert scratch_calls
        for name, scratch, bindings in scratch_calls:
            assert scratch  # clearing this metadata would break alias fallback
            assert any(k.startswith("scratch:") for k in bindings)
        assert not dev.inflight
    finally:
        dev.close()


def test_storage_snapshot_restores_all_forms_and_alias_metadata():
    dev = D.Device("offline", board="multimesh_v9t6")
    try:
        st = Storage(dev, 64)
        dev.write(st.addr, bytes(range(64)))
        key = ("fixture",)
        form = st.form(key, 32)
        dev.write(form.addr, bytes([77]) * 32)
        st.valid = {key}
        st.host, st.site, st.zeroed = b"host", ("producer", 32), {key}
        snapshot = StorageSnapshot(dev, {0: st, 1: StorageView(st, 2, 4)})
        assert len(snapshot.states) == 1
        dev.write(st.addr, bytes(64))
        dev.write(form.addr, bytes(32))
        st.wrote(FLAT)
        snapshot.restore()
        assert dev.read(st.addr, 64) == bytes(range(64))
        assert dev.read(form.addr, 32) == bytes([77]) * 32
        assert st.valid == {key} and st.host == b"host" and st.zeroed == {key}
        assert st.site == ("producer", 32)
    finally:
        dev.close()
