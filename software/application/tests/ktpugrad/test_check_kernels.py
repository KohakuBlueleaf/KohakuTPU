"""Independent CPU ASTs, error classes, margins, and a named mutation witness."""

import numpy as np
import pytest
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.uop import Ops
from tinygrad.uop.ops import AxisType, KernelInfo, ParamArg, UOp

from kohakutpu.application.checks.accuracy import (
    DEFAULT_INPUTS,
    PER_IMAGE_SECONDS,
    depth_budget,
    logit_report,
)
from kohakutpu.application.checks.kernels import (
    CPUKernel,
    compare_output,
    f16_storage,
    gamma,
    tolerance_class,
)
from kohakutpu.application.checks.step import StepChecker
from kohakutpu.application.ktpugrad import run
from kohakutpu.application.runtime import device as D


def param(slot, n):
    return UOp(
        Ops.PARAM,
        dtypes.half,
        src=(UOp.const(n),),
        arg=ParamArg(slot, dtypes.half, device="KTPU"),
    )


def ast_for(operation):
    n = 4
    out, a, b = (param(i, n) for i in range(3))
    r = UOp.range(n, 0, AxisType.WEAK)
    value = a.index(r)
    if operation == "add":
        value = value + b.index(r)
    elif operation == "sum":
        r = UOp.range(n, 0, AxisType.REDUCE)
        value = a.index(r).reduce(r, arg=(Ops.ADD, 0))
        return UOp.sink(
            out.index(UOp.const(0)).store(value), arg=KernelInfo(name="sum")
        )
    elif operation == "product":
        r = UOp.range(n, 0, AxisType.REDUCE)
        value = (a.index(r) * b.index(r)).reduce(r, arg=(Ops.ADD, 0))
        return UOp.sink(
            out.index(UOp.const(0)).store(value), arg=KernelInfo(name="product")
        )
    store = out.index(r).store(value)
    return UOp.sink(UOp(Ops.END, src=(store, r)), arg=KernelInfo(name=operation))


@pytest.mark.parametrize(
    "op,kind,category",
    [
        ("copy", "copy", "exact"),
        ("add", "vector", "elementwise"),
        ("sum", "vector", "reduction"),
        ("product", "matmul", "matmul"),
    ],
)
def test_cpu_ast_and_tolerance_classes(op, kind, category):
    ast = ast_for(op)
    assert tolerance_class(ast, kind) == category
    cpu = CPUKernel(ast)
    a = np.array([1, 2, 3, 4], np.float32)
    b = np.array([0.5, 2, -3, 1], np.float32)
    arrays = {0: np.zeros(4, np.float32), 1: a}
    if op in ("add", "product"):
        arrays[2] = b
    want, bound = cpu.reference(arrays, category, (1, 2) if op == "product" else ())
    # The witness expected values are literal math, not the KTPU evaluator.
    expected = {
        "copy": [1, 2, 3, 4],
        "add": [1.5, 4, 0, 5],
        "sum": [10, 0, 0, 0],
        "product": [-0.5, 0, 0, 0],
    }[op]
    np.testing.assert_array_equal(want[0], expected)
    assert np.isfinite(bound[0]).all()
    assert (bound[0] >= 0).all()
    n = 1 if op in ("sum", "product") else 4
    w = f16_storage(want[0][:n])
    check = compare_output(w, w, bound[0][:n], category)
    assert check["passed"]
    if category != "exact":
        assert bound[0][0] > 0


def test_cpu_keeps_same_indexing_and_f32_internal_precision():
    # Cancellation would vanish if every register operation were rounded to f16.
    out, a = param(0, 4), param(1, 4)
    r = UOp.range(4, 0, AxisType.WEAK)
    value = (a.index(3 - r) + 0.0001) - a.index(3 - r)
    ast = UOp.sink(
        UOp(Ops.END, src=(out.index(r).store(value), r)), arg=KernelInfo(name="reverse")
    )
    got = CPUKernel(ast).execute(
        CPUKernel(ast).ast, {0: np.zeros(4, np.float32), 1: np.ones(4, np.float32)}
    )
    assert np.all(got[0] > 0)
    np.testing.assert_allclose(got[0], 0.0001, atol=2e-8)


def test_exact_sign_and_ulp_histogram():
    want = np.array([1, -1, 0], np.float16)
    got = np.nextafter(want, np.float16(np.inf))
    report = compare_output(got, want, np.zeros(3), "exact")
    assert not report["passed"]
    assert report["ulp_f16"]["1"] == 3
    assert not compare_output(np.array([-0.0]), np.array([0.0]), [0], "exact")["passed"]


def test_bounds_are_per_element_not_a_global_relative_escape():
    want = np.array([100, 0], np.float16)
    assert not compare_output([100, 0.1], want, [1, 0.001], "elementwise")["passed"]
    assert gamma(4, 2**-15) > 4 * 2**-15
    assert np.isinf(gamma(32768, 2**-15))
    assert not compare_output([1], [1], [np.inf], "reduction")["passed"]


def test_f16_storage_edges():
    np.testing.assert_array_equal(f16_storage([70000, -70000]), [65504, -65504])
    assert f16_storage([2**-20])[0] != 0
    assert f16_storage([2**-20], drain=True)[0] == 0


@pytest.mark.parametrize(
    "natural_bad,auto", [(False, False), (True, False), (False, True)]
)
def test_named_corruption_witness_offline_memory(
    monkeypatch, tmp_path, natural_bad, auto
):
    # Offline memory supplies bytes, NOT a hardware-value oracle. The actual test
    # kernel is deliberately implemented by independent CPU tinygrad execution.
    original = run.execute
    with D.attached(D.Device("offline", board="vlt_v9t6s_1n")) as dev:
        dev.kind = "cpu_fixture"

        def cpu_kernel(device, plan, buffers):
            cpu = CPUKernel(plan.ast)
            arrays = {0: np.zeros(4, np.float32)}
            source = buffers[1]
            arrays[1] = np.frombuffer(device.read(source.addr, 8), np.float16).astype(
                np.float32
            )
            values = cpu.execute(cpu.ast, arrays)[0]
            if natural_bad:
                values[0] += 10
            dest = buffers[0]
            device.write(dest.addr, f16_storage(values).tobytes())
            return []

        monkeypatch.setattr(run, "execute", cpu_kernel)
        with StepChecker(
            dev, corrupt="auto" if auto else "vector", failure_dir=tmp_path
        ) as checker:
            checker.active = True
            x = Tensor(np.array([1, 2, 3, 4], np.float16), device="KTPU")
            (x.flip(0).contiguous() if auto else x + 1).realize()
        report = checker.report()
        assert not report["passed"]
        assert report["corrupted"]["name"] == report["rows"][0]["name"]
        assert report["rows"][0]["uncertifiable_elements"] == 1
        assert report["natural_failures"] == int(natural_bad)
        assert report["injected_failures"] == 1
        assert report["corrupted"]["element"] == int(natural_bad)
        assert report["corrupted"]["natural_element_passed"]
        assert report["corrupted"]["detected"]
        assert "diagnosis" in report["rows"][0]
        with np.load(report["rows"][0]["failure_arrays"], allow_pickle=False) as saved:
            assert np.isnan(saved["got"][int(natural_bad)])
            np.testing.assert_array_equal(saved["input_1"], [1, 2, 3, 4])
            np.testing.assert_array_equal(
                saved["want"], [4, 3, 2, 1] if auto else [2, 3, 4, 5]
            )
        dev.kind = "offline"
    assert run.execute is cpu_kernel
    assert original is not cpu_kernel


def test_decided_undecided_and_boundary():
    w = np.array(
        [[2, 1, 0, -1, -2, -3], [1, 1, 0, -1, -2, -3], [1, 0.8, 0, -1, -2, -3]],
        np.float64,
    )
    g = w.copy()
    g[0, 0] += 0.01
    g[1, 1] += 0.01
    g[2, 0] += 0.1
    result = logit_report(g, w)
    assert [r["decided"] for r in result["per_input"]] == [True, False, False]
    assert result["decided_top1_agreement"] == 1
    assert result["undecided_inputs"] == 2
    assert result["top5_set_agreement"] == 1
    assert result["per_input"][1]["cpu_top1"] == 0
    assert result["per_input"][1]["card_top1"] == 1
    # Reverting the strict 2*error rule would count the tied pair as evidence.
    assert logit_report([[1, 1]], [[1, 1]])["decided_top1_agreement"] is None


def test_top_five_is_a_set_and_not_ordered_rank():
    w = np.array([[6, 5, 4, 3, 2, 1]], np.float32)
    assert logit_report([[5, 6, 4, 3, 2, 1]], w)["top5_set_agreement"] == 1
    result = logit_report([[6, 5, 4, 3, 0, 2]], w)
    assert result["top5_set_agreement"] == 0
    assert result["top5_mean_overlap"] == 0.8


def test_metrics_cosine_nonfinite_and_depth_budget():
    assert logit_report([[0, 0]], [[0, 0]])["per_input"][0]["cosine_similarity"] == 1
    assert logit_report([[1, 0]], [[0, 1]])["per_input"][0]["cosine_similarity"] == 0
    with pytest.raises(ValueError, match="finite"):
        logit_report([[np.nan, 0]], [[1, 0]])
    budget = depth_budget(12, 0.034)
    assert 0.005 < budget["storage_only_relative_bound"] < 0.006
    assert not budget["within_storage_only_bound"]
    assert budget["full_network_bound"] is None
    assert 59 < DEFAULT_INPUTS * PER_IMAGE_SECONDS < 60


def test_tinyjit_replays_captured_runtimes_offline():
    from_jit = []
    with (
        D.attached(D.Device("offline", board="vlt_v9t6s_1n")) as dev,
        StepChecker(dev) as checker,
    ):

        @TinyJit
        def one(x):
            from_jit.append(1)
            return ((x + 2).realize() * 2).realize()

        with Context(TRAINING=1):
            for i in range(3):
                checker.active = i == 2
                one(Tensor(np.ones(4, np.float16), device="KTPU").realize())
        assert checker.graph_replays > 0
        assert checker.seen == 2
        assert len(from_jit) == 2
        assert checker.report()["passed"] is None
