"""Automatic call capture takes fresh inputs and refuses a graph that diverges."""

import gc
import weakref
from unittest.mock import patch

import numpy as np
import pytest
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp, _rebuild_dtype

from kohakutpu.application.ktpugrad.captured import call_capture as C
from kohakutpu.stack.rs import _rs


def expression(value, alternate=False):
    with Context(DEV="CPU"):
        source = Tensor(np.array(value, dtype=np.float32)).realize()
        result = source * 3 if alternate else source + 3
    return source, result


def test_fresh_inputs_outputs_and_first_divergence():
    table = C.Captures()
    outputs = []
    with patch.object(C.tensor, "transform_to_call", table.call):
        for alternate, value in ((False, [1, 2]), (False, [5, 7]), (True, [5, 7])):
            _source, result = expression(value, alternate)
            outputs.append(result.realize())
            np.testing.assert_array_equal(
                result.numpy(),
                np.array(value) * 3 if alternate else np.array(value) + 3,
            )
        np.testing.assert_array_equal(outputs[0].numpy(), [4, 5])
    assert table.counts["hit"] >= 1
    assert table.counts["miss"] >= 2
    assert outputs[0].uop is not outputs[1].uop


@pytest.mark.parametrize("native", [False, True])
def test_aliases_and_a_mutated_input_stay_exact(native):
    table = C.Captures(native=native)
    with Context(DEV="CPU"), patch.object(C.tensor, "transform_to_call", table.call):
        source = Tensor(np.arange(8, dtype=np.float32)).realize()
        first = (source[1:5] + source[1:5]).realize()
        np.testing.assert_array_equal(first.numpy(), [2, 4, 6, 8])
        source.assign(Tensor(np.arange(8, dtype=np.float32) + 10)).realize()
        second = (source[1:5] + source[1:5]).realize()
        np.testing.assert_array_equal(second.numpy(), [22, 24, 26, 28])
        np.testing.assert_array_equal(first.numpy(), [2, 4, 6, 8])
        shifted = (source[2:6] + source[1:5]).realize()
        np.testing.assert_array_equal(shifted.numpy(), [23, 25, 27, 29])
        other = Tensor(np.arange(8, dtype=np.float32) + 20).realize()
        distinct = (source[1:5] + other[1:5]).realize()
        np.testing.assert_array_equal(distinct.numpy(), [32, 34, 36, 38])
    assert table.counts["hit"] > 0


def test_placeholders_are_bounded(monkeypatch):
    monkeypatch.setattr(C, "KEPT", 2)
    table = C.Captures()
    unexpected = UOp(Ops.SINK, arg="not in original sink")
    with patch.object(C, "ORIGINAL", lambda sink: (sink, {unexpected: sink})):
        for size in range(200):
            source = UOp.new_buffer("CPU", size + 1, C.tensor.dtypes.float32)
            table.call(source.sink())
            assert len(table.placeholders) <= C.KEPT * 32
    assert not table.entries


def test_the_native_signature_keeps_topology_tags_and_layout():
    def signature(root):
        return _rs.capture_graph(root, Ops.BUFFER)[0]

    a = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    b = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    c = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    shared = UOp.sink(a, a)
    assert signature(shared) == signature(UOp.sink(c, c))
    assert signature(shared) != signature(UOp.sink(a, b))
    assert signature(shared) != signature(shared.replace(tag="different"))
    assert signature(UOp.sink(a)) != signature(
        UOp.sink(UOp.new_buffer("CPU", 16, a.dtype))
    )


def test_the_native_table_retains_no_cold_buffer():
    table = C.Captures(native=True)
    source = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    sink = UOp.sink(source)
    held = weakref.ref(source)
    with patch.object(C, "ORIGINAL", lambda root: (root, {root: root})):
        table.call(sink)
    del source, sink
    gc.collect()
    assert held() is None
    table.clear()
    assert not table.entries and not table.placeholders


def test_the_native_rewrite_matches_the_walk():
    a = UOp(Ops.SINK, arg="a")
    b = UOp(Ops.SINK, arg="b")
    shared = UOp.sink(a, a)
    opaque = UOp(Ops.FUNCTION, src=(shared, b))
    root = UOp.sink(opaque, shared, b)
    changed = {a: b, b: UOp.sink(a)}
    expected = root.substitute(changed, walk=True)
    actual = _rs.rewrite_roots(
        [root], changed, Ops.FUNCTION, Ops.CALL, UOp._OPAQUE_CALL_BODIES, _rebuild_dtype
    )[0]
    assert actual is expected
    assert actual.src[0].src[0] is shared
    assert actual.src[2] is changed[b]


def test_the_native_walk_sees_a_nested_shared_root():
    a = UOp(Ops.SINK, arg="a")
    b = UOp(Ops.SINK, arg="b")
    shared = UOp(Ops.SINK, src=(a, b))
    root = UOp(Ops.SINK, src=(shared, shared))
    assert _rs.affected_roots([root, b, a, root], {a: b}) == [0, 2, 3]
    assert _rs.affected_roots([root], {}) == []
