"""Identity and sysnode joins; these inputs expose the old _result/run zip."""

from types import SimpleNamespace as NS
from typing import Any

import pytest
import record
import retirement


def entry(name, kind, seq, nodes, **extra) -> dict[str, Any]:
    return dict(
        run=NS(name=name, kind=kind, wall=0.1, info={"seq": seq, "input": 3}),
        nodes={str(n): {"cycles": c, "list_index": 0} for n, c in nodes.items()},
        backend="eager",
        **extra,
    )


def test_multi_sysnode_rows_keep_launch_kind_each_node_and_max():
    entries = [
        entry("a", "quant", 10, {0: 11, 1: 17}),
        entry("b", "matmul", 11, {0: 31, 1: 23}),
    ]
    # Ring.rows() is node-major, NOT launch-major.
    ring = [
        {"node": n, "run": i, "start": 100 * i, "cycles": c}
        for n, values in ((0, (11, 31)), (1, (17, 23)))
        for i, c in enumerate(values)
    ]
    rows = record.joined_card_rows(entries, ring)
    assert [(r["name"], r["kind"], r["seq"], r["cycles"]) for r in rows] == [
        ("a", "quant", 10, 17),
        ("b", "matmul", 11, 31),
    ]
    assert [[v["cycles"] for v in r["nodes"].values()] for r in rows] == [
        [11, 17],
        [31, 23],
    ]
    assert [(r["node"], r["seq"], r["kind"]) for r in ring] == [
        (0, 10, "quant"),
        (0, 11, "matmul"),
        (1, 10, "quant"),
        (1, 11, "matmul"),
    ]


def test_missing_node_slot_is_refused_instead_of_shifting_the_next_launch():
    with pytest.raises(ValueError, match="underflow"):
        retirement.card_rows(
            [entry("a", "quant", 0, {0: 3, 1: 5})], [{"node": 0, "run": 0, "cycles": 3}]
        )
    with pytest.raises(ValueError, match="unmatched"):
        retirement.card_rows([], [{"node": 1, "run": 0, "cycles": 7}])


def test_completion_record_numeric_kind_cannot_overwrite_host_kind():
    e = entry(
        "a",
        "matmul",
        5,
        {0: 41},
        phases={"kind": 1, "seq": 999, "cycles": 100, "list_index": 9},
    )
    e["nodes"]["0"]["kind"] = 1
    row = retirement.card_rows([e])[0]
    assert (row["kind"], row["seq"], row["cycles"], row["list_index"]) == (
        "matmul",
        5,
        41,
        0,
    )
    assert row["completion_phases"]["kind"] == 1


def test_eager_list_members_take_one_ring_slot_and_only_first_takes_overhead():
    entries = [
        entry("q", "quant", 0, {1: 80}, deferred=True, first=True),
        entry("m", "matmul", 1, {1: 0}, deferred=True, first=False),
    ]
    for e in entries:
        e["nodes"]["1"].update(list_index=2, launches=2, head=10)
        e["phases"] = {"list_index": 2, "kind": 1}
    ring = [{"node": 1, "cycles": 23}, {"node": 1, "cycles": 47}]
    rows = retirement.card_rows(entries, ring)
    assert [r["cycles"] for r in rows] == [23, 47]
    assert [r["head"] for r in rows] == [10, 0]
    assert rows[0]["list_cycles"] == 80
    assert [r["list_index"] for r in rows] == [0, 0]
    assert [r["kind"] for r in rows] == ["quant", "matmul"]


def test_late_seq_assignment_is_read_at_serialization_not_retirement():
    e = entry("retained", "graph", None, {0: 40, 1: 60}, backend_unused=True)
    e["run"].info["seq"] = 17
    rows = retirement.card_rows([e])
    assert rows[0]["seq"] == 17
    assert rows[0]["max_node_cycles"] == 60


def test_single_node_identity_comes_from_placement_not_always_node_zero():
    submitted = NS(nodes=(3,), result={"cycles": 7, "phases": {"kind": 1}})
    assert retirement.node_results(submitted, NS(nodes=(0, 1, 2, 3))) == {
        "3": {"cycles": 7, "kind": 1}
    }
