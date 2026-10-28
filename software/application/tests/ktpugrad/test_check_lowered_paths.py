"""Lowering-aware budgets: CPU tinygrad staged arithmetic, never a card oracle."""

from types import SimpleNamespace

import numpy as np
import pytest
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import AxisType, KernelInfo, UOp

from kohakutpu.application.checks.kernels import CPUKernel, compare_output, f16_storage
from kohakutpu.application.checks.path import ArithmeticPath
from kohakutpu.application.ktpugrad import lower, run
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.storage import Storage


def sum_ast(n, columns=1, product=False):
    out = UOp.param(0, dtypes.half, (columns,), "KTPU")
    a = UOp.param(1, dtypes.half, (n * columns,), "KTPU")
    r = UOp.range(n, 0, AxisType.REDUCE)
    c = UOp.range(columns, 1, AxisType.WEAK)
    value = a.index(r * columns + c)
    if product:
        b = UOp.param(2, dtypes.half, (n * columns,), "KTPU")
        value = value * b.index(r * columns + c)
    value = value.reduce(r, arg=(Ops.ADD, 0))
    return UOp.sink(
        UOp(Ops.END, src=(out.index(c).store(value), c)),
        arg=KernelInfo(name="sum_path"),
    )


def tree_on_cpu(x, *, broken_pair=False, scale=1):
    value = Tensor(x.astype(np.float32), device="CPU")
    n = x.shape[0]
    while n > 1:
        half = n // 2
        top = value[:half]
        bottom = value[:half] if broken_pair else value[half:]
        value = (top + bottom).cast(dtypes.half).realize().cast(dtypes.float).realize()
        n = half
    return (value * scale).numpy().reshape(-1)


def test_f16_tree_storage_before_cancellation_kills_missing_budget():
    x = np.random.default_rng(19).normal(size=(8, 4096)).astype(np.float16)
    cpu = CPUKernel(sum_ast(8, 4096))
    arrays = {0: np.zeros(4096, np.float32), 1: x.astype(np.float32).ravel()}
    values, old = cpu.reference(arrays, "reduction")
    _, bound = cpu.reference(arrays, "reduction", path={"f16_stage_upper_bound": 4})
    want, got = f16_storage(values[0]), tree_on_cpu(x)
    assert not compare_output(got, want, old[0], "reduction")["passed"]
    assert compare_output(got, want, bound[0], "reduction")["passed"]


@pytest.mark.parametrize("broken_pair,scale", [(True, 1), (False, 0.25)])
def test_wrong_pairing_or_scale_still_fails(broken_pair, scale):
    x = np.arange(1, 9, dtype=np.float16).reshape(8, 1)
    cpu = CPUKernel(sum_ast(8))
    values, bounds = cpu.reference(
        {0: np.zeros(1, np.float32), 1: x.astype(np.float32).ravel()},
        "reduction",
        path={"f16_stage_upper_bound": 4},
    )
    got = tree_on_cpu(x, broken_pair=broken_pair, scale=scale)
    assert not compare_output(got, f16_storage(values[0]), bounds[0], "reduction")[
        "passed"
    ]


def test_late_cluster_product_gets_quant_budget_despite_vector_plan():
    n = 32
    x = np.full(n, 0.1, np.float32)
    x[0] = 1
    y = np.ones(n, np.float32)
    x, y = f16_storage(x).astype(np.float32), f16_storage(y).astype(np.float32)
    arrays = {0: np.zeros(1, np.float32), 1: x, 2: y}
    cpu = CPUKernel(sum_ast(n, product=True))
    values, old = cpu.reference(arrays, "reduction")
    path = dict(contains_mxfp7=True, max_cluster_blocks=1, f16_stage_upper_bound=3)
    _, bound = cpu.reference(arrays, "reduction", path=path)
    # A CPU perturbation within half the E5M3 scale, not a hardware emulator.
    perturbed = Tensor(x, device="CPU") + (9 / 512) * 0.49
    got = (perturbed * Tensor(y, device="CPU")).sum().numpy().reshape(-1)
    want = f16_storage(values[0])
    assert not compare_output(got, want, old[0], "reduction")["passed"]
    assert compare_output(got, want, bound[0], "reduction")["passed"]
    assert not compare_output(got * 2, want, bound[0], "reduction")["passed"]


def test_trace_metadata_is_selected_by_actual_runtime_path():
    dev = D.Device("offline", board="multimesh_v9t6")
    try:
        with D.attached(dev), ArithmeticPath(dev) as trace:
            x = Tensor(
                np.arange(8 * 32, dtype=np.float16).reshape(8, 32), device="KTPU"
            )
            x.sum(0).realize()
        path = trace.report()
        trees = [s for s in path["stages"] if s["stage"] == "column_tree"]
        assert trees and trees[0]["rows"] == 8 and trees[0]["levels"] == 3
        assert path["f16_stage_upper_bound"] >= 3
        assert not path["contains_mxfp7"]
    finally:
        dev.close()


def test_narrow_materialization_is_reported_and_declines_finite_budget():
    dev = D.Device("offline", board="multimesh_v9t6")
    try:
        with D.attached(dev), ArithmeticPath(dev) as trace:
            r = UOp.range(32, 0, AxisType.WEAK)
            params = [UOp.param(i, dtypes.half, (32,), "KTPU") for i in range(9)]
            value = sum(p.index(r) for p in params[1:])
            ast = UOp.sink(
                UOp(Ops.END, src=(params[0].index(r).store(value), r)),
                arg=KernelInfo(name="eight_inputs"),
            )
            buffers = {i: Storage(dev, 64) for i in range(9)}
            try:
                run.execute(dev, lower.plan(ast), buffers)
            finally:
                for storage in buffers.values():
                    storage.free()
        path = trace.report()
        assert any(s["stage"] == "narrow" for s in path["stages"])
        assert path["uncertifiable_reasons"]
        cpu = CPUKernel(sum_ast(8))
        values, bounds = cpu.reference(
            {0: np.zeros(1, np.float32), 1: np.ones(8, np.float32)},
            "reduction",
            path=path,
        )
        assert np.isinf(bounds[0]).all()
        assert not compare_output(values[0], values[0], bounds[0], "reduction")[
            "passed"
        ]
    finally:
        dev.close()


def test_computed_product_operand_refuses_original_buffer_scale(monkeypatch):
    # Only the observer's call boundary is exercised; no simulated value oracle.
    monkeypatch.setattr(run, "_product", lambda dev, plan, buffers: [])
    dev = object()
    plan = SimpleNamespace(
        mnk=tuple(UOp.range(n, i, AxisType.REDUCE) for i, n in enumerate((2, 2, 32))),
        a=SimpleNamespace(slot=("_tmp", 7)),
        b=SimpleNamespace(slot=2),
    )
    with ArithmeticPath(dev) as trace:
        run._product(dev, plan, {})
    path = trace.report()
    assert path["contains_mxfp7"] and path["uncertifiable_reasons"]
    cpu = CPUKernel(sum_ast(32, product=True))
    values, bounds = cpu.reference(
        {
            0: np.zeros(1, np.float32),
            1: np.ones(32, np.float32),
            2: np.ones(32, np.float32),
        },
        "reduction",
        path=path,
    )
    assert not compare_output(values[0], values[0], bounds[0], "reduction")["passed"]


def test_per_element_acceptance_cannot_be_recovered_from_maxima():
    # Both error arrays have max=1, but the location changes the verdict.
    want = np.zeros(2, np.float32)
    bounds = np.array([0.1, 2])
    assert not compare_output([1, 0], want, bounds, "reduction")["passed"]
    assert compare_output([0, 1], want, bounds, "reduction")["passed"]
