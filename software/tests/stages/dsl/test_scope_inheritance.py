"""Ordered loops retain the surrounding sysnode placement restriction."""

from kohakutpu.language import dsl as kt
from kohakutpu.language.dsl.trace import Trace, recording
from kohakutpu.language.dsl.types import Machine, TensorSpec
from kohakutpu.language.dsl.values import Param


def test_loop_preserves_explicit_node_scope():
    machine = Machine("ktpu.v9", 1, 2, 4, 16, 8, 16, "vector")
    trace = Trace("placement", machine)
    source = Param("source", TensorSpec((4, 32)))
    trace.params.append((source.name, source.spec))
    with recording(trace):
        for _node in kt.par(4, on=kt.node):
            for _iteration in kt.range(2):
                for _core in kt.par(4, on=kt.vc, at=kt.ANY):
                    kt.load(source)
    inner = trace.root[0].body[0].body[0]
    assert "vc[any]" in inner.head()
    assert inner.body[0].op == "load"
