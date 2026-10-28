"""Deferred producers precede immediate consumers, including form compaction."""

import numpy as np
import pytest

from kohakutpu.application.kernels import convert as C
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.storage import Storage


@pytest.mark.parametrize("mutant", [None, "early_consumer"])
def test_immediate_consumer_waits_for_deferred_writer(dev, mutant):
    source = Storage(dev, 256)
    intermediate = Storage(dev, 256)
    output = Storage(dev, 256)
    expected = np.arange(128, dtype=np.float16).reshape(4, 32) + np.float16(1)
    try:
        dev.write(source.addr, (expected - np.float16(1)).tobytes())
        dev.write(intermediate.addr, bytes(256))
        dev.write(output.addr, bytes(256))
        op = Operand("full", pitch=32)
        with (
            R.knobs(horizontal_fusion=True, mutant=mutant),
            R.multi_stage_scope(dev) as scope,
        ):
            scope.stages.append(
                (
                    "rows",
                    X.op("add", X.Load(0), 1.0),
                    (op,),
                    4,
                    32,
                    op,
                    {"in0": source.addr, "out": intermediate.addr},
                    {},
                )
            )
            kernel, given = C.copy_kernel(256)
            FM.launch(
                dev,
                kernel,
                given,
                {"src": intermediate.addr, "dst": output.addr},
                kind="copy",
            )
            R.flush_multi_stage(scope)
        actual = np.frombuffer(dev.read(output.addr, 256), np.float16)
        relative = (
            np.max(np.abs(actual.astype(float) - expected.ravel())) / expected.max()
        )
        if mutant:
            assert relative > 0.02  # NUM.TOL.007; not a rounding-order difference.
        else:
            assert relative == 0
    finally:
        for storage in (source, intermediate, output):
            storage.free()
