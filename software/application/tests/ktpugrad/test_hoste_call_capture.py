"""Automatic call capture substitutes fresh inputs and refuses graph divergence."""

import gc
import os
import weakref
from unittest.mock import patch

import numpy as np
import pytest
from tinygrad import Tensor, tensor
from tinygrad.helpers import Context
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp, _rebuild_dtype

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad import call_capture as C
from kohakutpu.application.ktpugrad import graph_walk, seam
from kohakutpu.application.runtime import device as D
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


def expression(value, alternate=False):
    with Context(DEV="CPU"):
        source = Tensor(np.array(value, dtype=np.float32)).realize()
        result = source * 3 if alternate else source + 3
    return source, result


def test_call_capture_fresh_inputs_outputs_and_first_divergence():
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


def mutation_witness(table):
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


def test_capture_alias_mutation_and_read_boundaries():
    mutation_witness(C.Captures())


def test_mutant_capture_reuses_cold_allocations_is_caught():
    assert "app.capture/reuses_cold" in rs.mutants()
    for native in (False, True):
        with rs.mutant("app.capture/reuses_cold"), pytest.raises(AssertionError):
            mutation_witness(C.Captures(native=native))


def test_capture_bounds_uncached_placeholders(monkeypatch):
    monkeypatch.setattr(C, "KEPT", 2)
    table = C.Captures()
    unexpected = UOp(Ops.SINK, arg="not in original sink")
    with patch.object(C, "ORIGINAL", lambda sink: (sink, {unexpected: sink})):
        for size in range(200):
            source = UOp.new_buffer("CPU", size + 1, C.tensor.dtypes.float32)
            table.call(source.sink())
            assert len(table.placeholders) <= C.KEPT * 32
    assert not table.entries


def test_native_capture_fresh_alias_mutation_and_divergence(monkeypatch):
    monkeypatch.setenv("KTPU_NATIVE_CAPTURE", "1")
    test_call_capture_fresh_inputs_outputs_and_first_divergence()
    mutation_witness(C.Captures())


def topology_witness(signature):
    a = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    b = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    c = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    shared = UOp.sink(a, a)
    assert signature(shared) == signature(UOp.sink(c, c))
    assert signature(shared) != signature(UOp.sink(a, b)), "alias topology collapsed"
    assert signature(shared) != signature(shared.replace(tag="different")), "tag lost"
    assert signature(UOp.sink(a)) != signature(
        UOp.sink(UOp.new_buffer("CPU", 16, a.dtype))
    ), "layout lost"


def test_native_capture_topology_and_tag_mutant():
    def signature(root):
        return _rs.capture_graph(root, Ops.BUFFER)[0]

    topology_witness(signature)
    assert "bindings.capture/drops_tag" in rs.mutants()
    with (
        rs.mutant("bindings.capture/drops_tag"),
        pytest.raises(AssertionError, match="tag lost"),
    ):
        topology_witness(signature)


def test_native_capture_retains_no_cold_buffer_and_clears(monkeypatch):
    monkeypatch.setenv("KTPU_NATIVE_CAPTURE", "1")
    table = C.Captures()
    source = UOp.new_buffer("CPU", 8, C.tensor.dtypes.float32)
    sink = UOp.sink(source)
    held = weakref.ref(source)
    with patch.object(C, "ORIGINAL", lambda root: (root, {root: root})):
        table.call(sink)
    del source, sink
    gc.collect()
    assert held() is None, "cold buffer identity retained by native recipe"
    table.clear()
    assert not table.entries and not table.placeholders


def test_native_rewrite_matches_walk_opaque_and_replacement_boundaries():
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
    # Replacement values are terminal; do not recursively rewrite b's new a.
    assert actual.src[2] is changed[b]


def test_knobs_off_keeps_tinygrad_hooks_and_latches_hot_path_env(monkeypatch):
    knobs = {
        "KTPU_EAGER_SCHEDULE",
        "KTPU_NATIVE_GRAPH_WALK",
        "KTPU_NATIVE_GRAPH_REWRITE",
        "KTPU_EAGER_CALL_CAPTURE",
        "KTPU_NATIVE_CAPTURE",
        "KTPU_AUTO_SEQUENCE",
        "KTPU_EAGER_EXEC",
        "KTPU_EAGER_RECIPE",
        "KTPU_NATIVE_RECIPE",
        "KTPU_WIRE_RECIPE",
        "KTPU_NATIVE_PROGRAM",
        "KTPU_HOST_RECIPE",
        "KTPU_NATIVE_MEMORY",
        "KTPU_NATIVE_DIRECT",
        "KTPU_NATIVE_QUEUE",
        "KTPU_NATIVE_POLL",
        "KTPU_NATIVE_EXCHANGE",
        "KOHAKUTPU_EAGER_FLUSH",
        "KTPU_SPECIALISE_CACHE",
        "KOHAKUTPU_RESIDENT",
        "KOHAKUTPU_EXECUTION_LIST",
    }
    for name in knobs:
        monkeypatch.delenv(name, raising=False)
    seam.install()
    assert tensor.create_linear_with_vars is seam._CREATE_LINEAR
    assert tensor._apply_map_to_tensors is graph_walk._ORIGINAL
    assert tensor.transform_to_call is C.ORIGINAL
    with D.attached(D.Device("offline", board="multimesh_v9t6", queue_depth=1)) as dev:
        kernel, given = V.flat_kernel(
            X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2
        )
        compiled = dev.specialise(kernel, given)
        names = compiled.l1.params(compiled.name, "l1")
        binds = {name: dev.alloc(4096) for name, _ in names}
        dev.run(compiled, binds)
        original_get = os.environ.get
        unlatched_upstream = {
            "KTPU_RS_MACHINE",
            "KTPU_VIEW_FORMS",
            "KTPU_ELIDE_CONTIGUOUS",
            "KTPU_HORIZONTAL_FUSION",
            "KTPU_PLAN_CACHE",
        }

        def forbid_run(key, default=None):
            if key != "KTPU_RS_MACHINE":
                raise AssertionError(f"hot-path os.environ.get({key!r})")
            return original_get(key, default)

        def forbid_realize(key, default=None):
            if key not in unlatched_upstream:
                raise AssertionError(f"hot-path os.environ.get({key!r})")
            return original_get(key, default)

        x = Tensor(np.ones((128,), dtype=np.float16), device="KTPU").realize()
        (x + 1).realize()

        def exercise_hot_path():
            with patch.object(os.environ, "get", side_effect=forbid_run):
                dev.run(compiled, binds)
            with patch.object(os.environ, "get", side_effect=forbid_realize):
                (x + 1).realize()

        exercise_hot_path()
        for name, knob in (
            ("app.device/unlatched_resident", "KOHAKUTPU_RESIDENT"),
            ("app.sequence/unlatched", "KTPU_AUTO_SEQUENCE"),
        ):
            assert name in rs.mutants()
            with rs.mutant(name), pytest.raises(AssertionError, match=knob):
                exercise_hot_path()
        dev.close()


def reachability_witness(walk):
    a = UOp(Ops.SINK, arg="a")
    b = UOp(Ops.SINK, arg="b")
    shared = UOp(Ops.SINK, src=(a, b))
    root = UOp(Ops.SINK, src=(shared, shared))
    assert walk([root, b, a, root], {a: b}) == [
        0,
        2,
        3,
    ], "nested shared dependency lost"
    assert walk([root], {}) == []


def test_native_graph_walk_nested_shared_roots():
    reachability_witness(_rs.affected_roots)


def test_mutant_native_graph_walk_only_checks_roots_is_caught():
    assert "bindings.walk/roots_only" in rs.mutants()
    with (
        rs.mutant("bindings.walk/roots_only"),
        pytest.raises(AssertionError, match="nested"),
    ):
        reachability_witness(_rs.affected_roots)
