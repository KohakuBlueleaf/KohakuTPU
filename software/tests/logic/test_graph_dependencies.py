"""Semantic graph preparation orders allocation hazards, not capture positions."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.graph.capture import Graph, Launch, Span, Template

TEMPLATE = Template(("access",), "elementwise", lambda parameters, addresses: addresses)


def launch(inputs, output):
    return Launch(TEMPLATE, None, tuple(inputs), output)


def prepare(launches, addresses):
    runner = SimpleNamespace(prepare=lambda packets, edges: edges)
    return Graph(tuple(launches), len(launches), (), 0.0).prepare(runner, addresses)


@pytest.mark.parametrize("hazard", ["raw", "war", "waw", "read-read", "disjoint"])
def test_only_conflicting_allocation_lines_add_edges(hazard):
    a = Span("a", 0, 16)
    alias = Span("a", 16 if hazard != "disjoint" else 64, 16)
    x, y, z = (Span(name, 0, 64) for name in ("x", "y", "z"))
    first = launch((a,), x) if hazard in ("war", "read-read") else launch((), a)
    last = (
        launch((alias,), y)
        if hazard in ("raw", "read-read", "disjoint")
        else launch((), alias)
    )
    edges = prepare(
        (first, launch((), z), last),
        {"a": 0x1000, "x": 0x2000, "y": 0x3000, "z": 0x4000},
    )
    assert edges == ((), (), () if hazard in ("read-read", "disjoint") else (0,))


def test_a_write_joins_readers_and_a_consumer_uses_the_latest_frontier():
    a, b, c, d = (Span(name, 0, 64) for name in ("a", "b", "c", "d"))
    edges = prepare(
        (
            launch((), a),
            launch((a,), b),
            launch((a,), c),
            launch((), a),
            launch((a, b, c), d),
        ),
        {"a": 0x1000, "b": 0x2000, "c": 0x3000, "d": 0x4000},
    )
    assert edges == ((), (0,), (0,), (1, 2), (3,))
