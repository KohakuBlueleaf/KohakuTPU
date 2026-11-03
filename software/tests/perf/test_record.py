"""The card's records against the runs they belong to: one row a run, whatever
went out as a list."""

from types import SimpleNamespace as NS

import record


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


def test_without_a_ring_a_list_keeps_each_runs_own_cycles():
    runs = [
        NS(name="b", kind="quant", wall=0.2, cycles=30, info={}),
        NS(name="c", kind="matmul", wall=0.3, cycles=0, info={}),
    ]
    rows = record.card_rows(runs, [dict(cycles=30, list_index=2, item_tag=1)], None)
    assert [(r["name"], r["cycles"]) for r in rows] == [("b", 30), ("c", 0)]
