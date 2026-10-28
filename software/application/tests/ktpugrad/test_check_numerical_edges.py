"""Policy edges have finite-error witnesses independent of any card model."""

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import KernelInfo, ParamArg, UOp

from kohakutpu.application.checks.kernels import (
    CPUKernel,
    compare_output,
    tolerance_class,
)
from kohakutpu.application.checks.step import StepChecker
from kohakutpu.application.ktpugrad import lower, run
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.storage import Storage


def scalar_kernel(value):
    out = UOp(
        Ops.PARAM,
        dtypes.float,
        src=(UOp.const(1),),
        arg=ParamArg(0, dtypes.float, device="KTPU"),
    )
    return UOp.sink(out.index(UOp.const(0)).store(value), arg=KernelInfo(name="edge"))


def test_arbitrary_f32_immediate_is_not_movement_exact():
    value = UOp.const(1 + 2**-11 + 2**-17, dtypes.float)
    ast = scalar_kernel(value)
    assert tolerance_class(ast, "vector") == "elementwise"
    values, bounds = CPUKernel(ast).reference(
        {0: np.zeros(1, np.float32)}, "elementwise"
    )
    assert bounds[0][0] > 0
    assert values[0][0] > 1


def test_division_seed_policy_covers_a_several_ulp_result():
    a = UOp(
        Ops.PARAM,
        dtypes.float,
        src=(UOp.const(1),),
        arg=ParamArg(1, dtypes.float, device="KTPU"),
    )
    value = UOp(
        Ops.FDIV,
        dtypes.float,
        src=(UOp.const(1.0, dtypes.float), a.index(UOp.const(0))),
    )
    ast = scalar_kernel(value)
    values, bounds = CPUKernel(ast).reference(
        {0: np.zeros(1, np.float32), 1: np.ones(1, np.float32)}, "elementwise"
    )
    assert bounds[0][0] >= 2 * 2**-11 + 9 * 2**-15
    assert compare_output(values[0], values[0], bounds[0], "elementwise")["passed"]


def test_finite_corruption_is_rejected_for_every_tolerance_class():
    for category in ("exact", "elementwise", "reduction", "matmul"):
        report = compare_output([2.0, -3.0], [1.0, -3.0], [0.01, 0.01], category)
        assert not report["passed"], category
        assert report["max_absolute_error"] == 1.0
        assert report["failing_elements"] == 1


def test_sqrt_zero_input_with_uncertainty_needs_a_nonzero_budget():
    a = UOp(
        Ops.PARAM,
        dtypes.float,
        src=(UOp.const(1),),
        arg=ParamArg(1, dtypes.float, device="KTPU"),
    )
    # Cancellation before sqrt: the input can move away from zero.
    x = a.index(UOp.const(0)) - 1
    ast = scalar_kernel(x.sqrt())
    _, bounds = CPUKernel(ast).reference(
        {0: np.zeros(1, np.float32), 1: np.ones(1, np.float32)}, "elementwise"
    )
    assert np.isinf(bounds[0][0])


def test_snapshot_is_actual_device_bytes_even_for_inplace_output(monkeypatch):
    a = UOp(
        Ops.PARAM,
        dtypes.half,
        src=(UOp.const(1),),
        arg=ParamArg(0, dtypes.half, device="KTPU"),
    )
    at = a.index(UOp.const(0))
    ast = UOp.sink(at.store(at + 1), arg=KernelInfo(name="inplace_actual_bytes"))

    def fixture_target(device, plan, buffers):
        # A one-element CPU fixture target; never used as the reference oracle.
        address = buffers[0].addr
        value = np.frombuffer(device.read(address, 2), np.float16).copy()
        device.write(address, (value + np.float16(1)).tobytes())
        return []

    monkeypatch.setattr(run, "execute", fixture_target)
    dev = D.Device("offline", board="vlt_v9t6s_1n")
    storage = Storage(dev, 2)
    try:
        # Stale host provenance intentionally disagrees with actual device bytes.
        storage.host = np.array([1], np.float16).tobytes()
        dev.write(storage.addr, np.array([7], np.float16).tobytes())
        dev.kind = "cpu_fixture"
        with D.attached(dev), StepChecker(dev) as checker:
            checker.active = True
            run.execute(dev, lower.plan(ast), {0: storage})
        assert checker.report()["passed"]
        assert checker.rows[0]["name"] == "inplace_actual_bytes"
        assert np.frombuffer(dev.read(storage.addr, 2), np.float16)[0] == 8
    finally:
        storage.free()
        dev.kind = "offline"
        dev.close()
