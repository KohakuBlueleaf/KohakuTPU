"""Rebinding never changes a definition that another statement already uses."""

import pytest

from kohakutpu.language.dsl.errors import DSLError
from kohakutpu.language.dsl.trace import Trace
from kohakutpu.language.dsl.types import Machine
from kohakutpu.language.dsl.values import Value


def value(trace, name):
    stmt = trace.emit("add", "input", "1", target=name)
    return Value(trace, name, "f32", (4, 32), "reg", stmt=stmt)


def test_assignment_refuses_used_definition():
    trace = Trace("assignment", Machine("ktpu.v9", 1, 2, 4, 16, 8, 16, "vector"))
    old = value(trace, "old")
    new = value(trace, "new")
    trace.emit("mul", "new", "2", target="use")
    with pytest.raises(DSLError, match="active block"):
        old.assign(new)
    assert new.stmt.target == "new"


def test_assignment_refuses_definition_in_another_block():
    trace = Trace("assignment", Machine("ktpu.v9", 1, 2, 4, 16, 8, 16, "vector"))
    old = value(trace, "old")
    block = trace.emit("if", "sticky")
    with trace.block(block):
        new = value(trace, "new")
    with pytest.raises(DSLError, match="active block"):
        old.assign(new)
