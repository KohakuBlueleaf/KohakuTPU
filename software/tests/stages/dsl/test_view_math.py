"""Sliced operands participate in vector operations and admission."""

import pytest

from kohakutpu.language import dsl as kt
from kohakutpu.language.dsl import ops
from kohakutpu.language.dsl.errors import DSLError
from kohakutpu.language.dsl.trace import Trace, recording
from kohakutpu.language.dsl.types import Machine
from kohakutpu.language.dsl.values import Value


def test_sliced_register_is_used_by_math():
    trace = Trace("sliced", Machine("ktpu.v9", 1, 2, 4, 16, 8, 16, "vector"))
    value = Value(trace, "input", "f32", (4, 64), "reg", "gt4")
    result = None
    with recording(trace):
        for _core in kt.par(2, on=kt.vc):
            result = ops.math("add", value[:, 0:32], 1)
            ops.reduce("rsum", result)
    statements = trace.root[0].body
    assert "input[:, 0:32]" in statements[0].head()
    assert result is not None
    assert result.name in statements[1].head()


@pytest.mark.parametrize("space,mode", [("A", "gt4"), ("reg", None)])
def test_view_cannot_bypass_operand_admission(space, mode):
    trace = Trace("admission", Machine("ktpu.v9", 1, 2, 4, 16, 8, 16, "vector"))
    left = Value(trace, "left", "f32", (4, 32), "reg", "gt4")
    right = Value(trace, "right", "f32", (4, 32), space, mode)
    with recording(trace):
        for _core in kt.par(2, on=kt.vc):
            with pytest.raises(DSLError):
                ops.math("add", left, right[:, :])
            ops.math("add", left, 1)


def test_l1_view_is_unpacked_before_math():
    trace = Trace("unpacking", Machine("ktpu.v9", 1, 2, 4, 16, 8, 16, "vector"))
    value = Value(trace, "input", "f16", (8, 32), "l1", "gt4")
    with recording(trace):
        for _core in kt.par(2, on=kt.vc):
            ops.math("add", value[0:4, :], 1)
    unpack, add = trace.root[0].body
    assert unpack.op == "unpack"
    assert "input[0 : +4, :]" in unpack.head()
    assert unpack.target in add.head()
