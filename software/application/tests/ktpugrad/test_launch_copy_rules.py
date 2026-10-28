"""Each elision witness fails its launch-count property when its rule is disabled."""

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import pytest
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import launch_rules
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.storage import Storage


@pytest.mark.parametrize("rule", ["self_copy", "identity_walk"])
def test_identity_elision_mutants(rule):
    counts = []
    for mutant in (False, True):
        dev = D.Device("offline", board="multimesh_v9t6")
        st = Storage(dev, 256)
        axis = UOp.range(128, 0)
        acc = Access(1, dtypes.half, ((axis, 1),), 0)
        plan = Plan(
            "copy",
            UOp(Ops.SINK),
            "identity",
            0,
            dtypes.half,
            (128,),
            (axis,),
            X.Load(0),
            [acc],
        )
        try:
            with (
                R.knobs(elide_contiguous=True),
                launch_rules.keep_copy(rule) if mutant else nullcontext(),
            ):
                (R._copy if rule == "self_copy" else R._move)(dev, plan, {0: st, 1: st})
            counts.append(len(dev.runs))
        finally:
            st.free()
            dev.close()
    assert counts == [0, 1]


@pytest.mark.parametrize(
    "rule,n", [("product_rows", 84), ("product_flat", 192), ("padded_epilogue", 192)]
)
def test_product_elision_mutants(rule, n):
    counts = []
    for mutant in (False, True):
        dev = D.Device("offline", board="multimesh_v9t6")
        try:
            with (
                D.attached(dev),
                R.knobs(elide_contiguous=True),
                launch_rules.keep_copy(rule) if mutant else nullcontext(),
            ):
                x = Tensor(np.ones((197, 64), np.float16), device="KTPU").realize()
                w = Tensor(np.ones((64, n), np.float16), device="KTPU").realize()
                extra = None
                if rule == "padded_epilogue":
                    extra = (x @ w).realize()
                mark = len(dev.runs)
                result = x @ w
                if extra is not None:
                    result = result + extra
                result.realize()
                counts.append(len(dev.runs) - mark)
        finally:
            dev.close()
    assert counts[1] > counts[0], (rule, counts)


@pytest.mark.parametrize("rule", ["dense_repitch", "store_hazard"])
def test_training_copy_rule_mutants(rule):
    root = Path(__file__).resolve().parents[4]
    counts = []
    # tinygrad's schedule cache must not carry a fixed/mutant plan between trials.
    for selected in ("none", rule):
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/py/check/launch_copy_rules.py"),
                selected,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=100,
        )
        counts.append(json.loads(result.stdout.splitlines()[-1])["runs"])
    assert counts[1] > counts[0], (rule, counts)
