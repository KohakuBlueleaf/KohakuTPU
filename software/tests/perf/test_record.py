"""The card's records against the runs they belong to: one row a run, whatever
went out as a list."""

from types import SimpleNamespace as NS

import pytest
import record
import traces

from kohakutpu.application.runtime import device as D


def test_an_eager_list_is_a_row_a_run_with_its_own_ring_cycles():
    runs = [
        NS(name="a", kind="dsl", wall=0.1, cycles=5, info=dict(seq=0)),
        NS(name="b", kind="quant", wall=0.2, cycles=30, info=dict(seq=1)),
        NS(name="c", kind="matmul", wall=0.3, cycles=0, info=dict(seq=2)),
        NS(name="d", kind="dsl", wall=0.4, cycles=0, info=dict(seq=3)),
        NS(name="replay", kind="replay", wall=0.5, cycles=9, info=dict(seq=4)),
        NS(name="e", kind="dsl", wall=0.6, cycles=7, info=dict(seq=5)),
    ]
    head = dict(head=700, bind=500, barrier=170, idle=4000, item_tag=3, epoch=0)
    records = [
        dict(head, cycles=5, steps=4, list_index=0),
        dict(head, cycles=30, steps=29, list_index=3),
        dict(head, cycles=9, steps=8, list_index=2),
        dict(head, cycles=7, steps=6, list_index=0),
    ]
    ring = [5, 10, 12, 8, 4, 5, 7]

    rows = record.card_rows(runs, records, ring)

    assert [r["name"] for r in rows] == ["a", "b", "c", "d", "replay", "e"]
    assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert [r["cycles"] for r in rows] == [5, 10, 12, 8, 9, 7]
    assert [r["steps"] for r in rows[1:4]] == [10, 12, 8]
    assert rows[1]["list_cycles"] == 30 and rows[1]["head"] == 700
    assert rows[2]["head"] == 0 and rows[2]["idle"] == 0
    assert rows[4]["list_index"] == 2


def _kernel(name):
    return NS(name=name, l2_text="", l1_text="", scratch=[])


@pytest.fixture
def wrapped(tmp_path, monkeypatch):
    """`record.recording` around a fake submit (one clock tick a reading): an
    "outer" launch flushes two staged "inner" ones, a "bad" one raises."""
    for name in ("submit", "replay", "compile_graph"):
        monkeypatch.setattr(D.Device, name, getattr(D.Device, name))
    ticks = iter(range(1000))
    monkeypatch.setattr(record.time, "perf_counter", lambda: next(ticks) / 1000)

    def fake(dev, compiled, binds, kind="dsl", info=None):
        if compiled.name == "outer":
            D.Device.submit(dev, _kernel("inner"), {"y": 128})
            D.Device.submit(dev, _kernel("inner"), {"y": 256})
        if compiled.name == "bad":
            raise RuntimeError("refused")
        return NS(info={})

    monkeypatch.setattr(D.Device, "submit", fake)
    trace = record.Trace(tmp_path)
    record.recording(trace, None, False, record.Profile(None))
    return trace, NS(mmu=NS(resolve=lambda a: (0, a)), recording=False)


def test_launches_flushed_inside_a_submit_are_nested_events(wrapped, tmp_path):
    """Two staged launches flushed inside a submit: three events, their own
    `seq`s, the inner naming the outer, no gap charged inside the outer, and
    the host times summing to the clock's span."""
    trace, dev = wrapped
    got = D.Device.submit(dev, _kernel("outer"), {"x": 64})
    trace.save({})

    rows = traces.events(tmp_path)
    assert got.info["seq"] == 0
    assert [(r["name"], r["seq"], r.get("parent")) for r in rows] == [
        ("outer", 0, None),
        ("inner", 1, 0),
        ("inner", 2, 0),
    ]
    assert [r["gap_ms"] for r in rows[1:]] == [0.0, 0.0]
    # Readings 0..5 ms: outer in, each inner in and out, outer out.
    assert [r["submit_ms"] for r in rows] == [3.0, 1.0, 1.0]
    assert sum(r["submit_ms"] + r["gap_ms"] for r in rows) == 5.0


def test_a_submit_that_raises_closes_its_event(wrapped):
    trace, dev = wrapped
    with pytest.raises(RuntimeError):
        D.Device.submit(dev, _kernel("bad"), {"x": 64})
    assert trace.open == []
    D.Device.submit(dev, _kernel("outer"), {"x": 64})
    assert trace.open == []


def test_without_a_ring_a_list_keeps_each_runs_own_cycles():
    runs = [
        NS(name="b", kind="quant", wall=0.2, cycles=30, info={}),
        NS(name="c", kind="matmul", wall=0.3, cycles=0, info={}),
    ]
    rows = record.card_rows(runs, [dict(cycles=30, list_index=2, item_tag=1)], None)
    assert [(r["name"], r["cycles"]) for r in rows] == [("b", 30), ("c", 0)]
