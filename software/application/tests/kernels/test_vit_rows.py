"""Logical row reductions and tails compile through the refusing verifier."""

import numpy as np
import pytest

from kohakutpu.application import api as kp
from kohakutpu.application.api.tensor import Unsupported
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.runtime import device as D
from kohakutpu.stack import rs

ROWS = (*range(1, 66), 197)


def test_product_rows_kill_no_serial_loop():
    test_logical_rows(197, 197, "prod")
    with (
        rs.mutant("l2.vector/no-serial-loop"),
        pytest.raises(Unsupported, match=r'VLOOP: field "body"'),
    ):
        test_logical_rows(197, 197, "prod")


@pytest.mark.parametrize("rows", ROWS)
@pytest.mark.parametrize("width", [128, 197])
@pytest.mark.parametrize(
    "op", ["softmax", "layernorm", "sum", "max", "prod", "elementwise"]
)
def test_logical_rows(rows, width, op):
    with D.attached(D.Device(kind="offline", board="multimesh_v9t6")) as dev:
        a = np.random.default_rng(17).standard_normal((rows, width)).astype(np.float16)
        x = kp.tensor(a)
        y = (
            x + 1
            if op == "elementwise"
            else getattr(x, op)(
                **({"axis": -1} if op in ("sum", "max", "prod") else {})
            )
        )
        y.realize()
        assert dev.runs
        assert y.shape == ((rows,) if op in ("sum", "max", "prod") else (rows, width))
        if width % 32:
            assert y.st.nbytes == 2 * y.size


def test_vit_softmax_true_shape():
    with D.attached(D.Device(kind="offline", board="multimesh_v9t6")) as dev:
        a = np.random.default_rng(17).standard_normal((3, 197, 197)).astype(np.float16)
        kp.tensor(a).softmax().realize()
        assert dev.runs
        assert not any(run.name == "flat_f3cb4822f6" for run in dev.runs)


def test_reduction_axes_are_logical_columns(monkeypatch):
    original = R.execute
    plans = []

    def capture(dev, plan, bufs):
        plans.append(plan)
        return original(dev, plan, bufs)

    monkeypatch.setattr(R, "execute", capture)
    with D.attached(D.Device(kind="offline", board="multimesh_v9t6")):
        a = np.random.default_rng(19).standard_normal((3, 197, 197)).astype(np.float16)
        y = kp.tensor(a).softmax().realize()
        assert y.st.nbytes == a.nbytes
    reduced = [p for p in plans if p.reduce_ranges]
    assert len(reduced) == 2
    for plan in reduced:
        assert tuple(U.extent(r) for r in plan.out_ranges) == (3, 197)
        assert len(plan.reduce_ranges) == 1
        col = plan.reduce_ranges[0]
        assert U.extent(col) == 197
        assert col not in plan.out_ranges
        assert all(rs == (col,) for rs in plan.reductions.values())
        assert any(a.c(col) == 1 for a in plan.accesses)
