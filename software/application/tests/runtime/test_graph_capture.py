"""Neighbour capture contracts; CPU numerics are not native kernel certification."""

from dataclasses import replace

import numpy as np
import pytest
from examples.graph.capture_measure import ADD, Runner, case, library

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.application.runtime.graph.capture import (
    Capture,
    Launch,
    Library,
    Rules,
    Span,
)


@pytest.mark.parametrize("rule", ["elementwise", "quant", "layout", "norm"])
def test_rule_toggle_class_reuse_and_reference_numerics(rule):
    lib = library()
    cap, out, addresses, memory = case(rule)
    off = cap.finish((out,), lib, Rules(**{rule: False}))
    on = cap.finish((out,), lib)
    assert len(off.launches) == 2 and len(on.launches) == 1
    expected = memory[addresses[out.allocation]].copy()
    runner = Runner(memory)
    prepared = on.prepare(runner, addresses)
    prepared.submit()
    assert runner.submissions == 1 and runner.stores == 1
    np.testing.assert_array_equal(
        memory[addresses[out.allocation]].view(np.uint16), expected.view(np.uint16)
    )
    other, out2, addresses2, memory2 = case(rule, rows=7, base=100)
    other_expected = memory2[addresses2[out2.allocation]].copy()
    other_graph = other.finish((out2,), lib)
    other_graph.prepare(Runner(memory2), addresses2).submit()
    np.testing.assert_array_equal(memory2[addresses2[out2.allocation]], other_expected)
    assert other_graph.launches[0].template is on.launches[0].template
    assert lib.builds == 1 and lib.hits == 1
    # The witness fails when native composition admission is removed.
    assert len(cap.finish((out,), Library()).launches) == 2


def test_observable_and_aliased_intermediates_are_not_removed(monkeypatch):
    cap, out, _, _ = case("elementwise")
    temporary = cap.launches[0].output
    exported = Span(temporary.allocation, 2, 2)
    assert len(cap.finish((out, exported), library()).launches) == 2
    with monkeypatch.context() as mutation:
        mutation.setattr(Span, "overlaps", lambda self, other: False)
        assert len(cap.finish((out, exported), library()).launches) == 1
    reader = Launch(ADD, 1, (exported,), Span("another-output", 0, 2))
    cap.launches.append(reader)
    assert len(cap.finish((out,), library()).launches) == 3


def test_partial_consumer_and_inplace_aliases_are_barriers():
    cap, out, _, _ = case("elementwise")
    left, right = cap.launches
    partial = Span(left.output.allocation, 2, 2)
    cap.launches[1] = replace(right, inputs=right.inputs + (partial,))
    assert len(cap.finish((out,), library()).launches) == 2
    cap.launches[1] = replace(right, output=left.inputs[0])
    assert len(cap.finish((), library()).launches) == 2


def test_disjoint_arena_views_use_bounded_overlap_queries(monkeypatch):
    cap = Capture()
    for i in range(256):
        pair, _, _, _ = case("elementwise", base=i * 4)
        for launch in pair.launches:
            spans = tuple(
                Span("arena", span.allocation * 4096, span.nbytes)
                for span in (*launch.inputs, launch.output)
            )
            cap.launches.append(replace(launch, inputs=spans[:-1], output=spans[-1]))
    calls = 0
    original = Span.overlaps

    def counted(self, other):
        nonlocal calls
        calls += 1
        return original(self, other)

    monkeypatch.setattr(Span, "overlaps", counted)
    graph = cap.finish((), library())
    assert len(graph.launches) == 256
    assert calls < 256 * 20


def test_capture_retains_native_kernel_and_bindings_and_rejects_host_effects():
    dev = Device(kind="offline", board="multimesh_v9t6")
    x, y = dev.alloc(256), dev.alloc(256)
    dev.record()
    run = generated.flat(
        dev, X.op("add", X.Load(0), 1.0), ("f16",), 128, {"in0": x, "out": y}
    )
    log = dev.recorded()
    assert log.templates == ((run.info["compiled"], (("in0", x), ("out", y))),)
    seen = []
    Capture.from_recorded(log, lambda kernel, bindings: seen.append((kernel, bindings)))
    assert seen == list(log.templates)
    effect = Recorded([("write", x, bytes(32))], [], dev)
    with pytest.raises(ValueError, match="run-only"):
        Capture.from_recorded(effect, lambda *_: None)
    missing = Recorded([("run", {})], [], dev)
    with pytest.raises(ValueError, match="one template per run"):
        Capture.from_recorded(missing, lambda *_: None)
    log.close()
    effect.close()
    missing.close()
    dev.free(x)
    dev.free(y)
    dev.close()
