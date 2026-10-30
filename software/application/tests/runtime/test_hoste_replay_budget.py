"""Prepared replay pins hot packages within budget and streams the remaining runs."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime import packages
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.application.runtime.replay_budget import (
    DEFAULT_FRACTION,
    held_bytes,
    select,
)
from kohakutpu.stack import rs

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)


def setup():
    mem = HostMemory()
    memory = CardMemory((1 << 20, 2 << 20), {})
    queue = NodeQueue(
        mem,
        0,
        sq_n=4,
        cq_n=4,
        spill=lambda size: memory.alloc(size, owner="package"),
        unspill=memory.free,
        record=LAYOUT.record,
        layout=LAYOUT,
    )
    queue.init()
    firmware = SpecFirmware(mem, 0, record=LAYOUT.record, layout=LAYOUT)
    firmware.boot()
    queue.idle = firmware.step
    card = SimpleNamespace(
        queues={0: queue},
        memory=memory,
        machine_json="{}",
        queue_layout=LAYOUT,
        admission_cached=True,
    )
    a, b, c = (bytes([i]) * 128 for i in (1, 2, 3))
    log = [("run", {0: (raw, ())}) for raw in (a, b, a, c, a)]
    return card, queue, firmware, log


def witness():
    card, queue, firmware, log = setup()
    with (
        patch.object(packages, "admit"),
        patch.object(packages, "Package", lambda *args: None),
    ):
        full = NodeCard.prepare_recorded(card, log, 1024)
        assert full.selection["pinned_packages"] == 3
        assert full.selection["streamed_calls"] == 0
        full.close()
        prepared = NodeCard.prepare_recorded(card, log, 256)
    assert queue.prepared_bytes == 128
    assert prepared.selection["pinned_packages"] == 1
    assert prepared.selection["streamed_calls"] == 2
    for _, programs in prepared:
        upload, _, binds = programs[0]
        queue.wait(queue.submit(upload, binds))
    assert queue.counters["submitted"] == 5
    assert len([event for event in firmware.events if event[0] == "start"]) == 5
    prepared.close()
    assert queue.prepared_bytes == 0


def test_overbudget_replay_streams_cold_packages():
    witness()


def test_mutant_old_budget_refusal_fails_same_witness():
    assert "app.replay/refuses_unpinned" in rs.mutants()
    with (
        rs.mutant("app.replay/refuses_unpinned"),
        pytest.raises(OutOfMemory, match="aggregate packages"),
    ):
        witness()


def test_auto_budget_uses_reservable_capacity_and_shared_pins():
    card, queue, _, log = setup()
    expected = int(card.memory.dram.free * DEFAULT_FRACTION)
    budget, chosen, _, _ = select(card, log, None, 1)
    assert budget == expected and len(chosen) == 3
    quarter, _, _, _ = select(card, log, None, 1, fraction=0.25)
    assert quarter == card.memory.dram.free // 4
    owned = queue.prepare(log[0][1][0][0])
    budget, chosen, _, _ = select(card, log, 256, 1)
    assert budget == 256 and set(chosen) == {(0, log[0][1][0][0])}
    owned.close()


def test_zero_pin_budget_preserves_list_replay_as_streamed_runs():
    card, queue, _, log = setup()
    with (
        patch.object(packages, "admit"),
        patch.object(packages, "Package", lambda *args: None),
    ):
        prepared = NodeCard.prepare_recorded(card, log, 0)
    events, groups = NodeCard.prepare_execution(card, prepared)
    assert all(event[0] == "run" for event in events)
    assert groups == [(i,) for i in range(5)] and queue.prepared_bytes == 0
    prepared.close()


def test_pin_selection_leaves_space_for_larger_streamed_package():
    card, _, _, _ = setup()
    a, b = bytes(512 << 10), bytes(768 << 10)
    log = [("run", {0: (raw, ())}) for raw in (a, a, b)]
    _, selected, _, workspace = select(card, log, None, 1)
    assert not selected and workspace == [768 << 10]
    with pytest.raises(OutOfMemory, match="workspace"):
        select(card, [("run", {0: (bytes(2 << 20), ())})], None, 1)
    _, selected, _, _ = select(
        card, [("run", {0: (bytes(2 << 20), ())})], None, 1, overcommit=True
    )
    assert not selected


def test_fragmented_pins_follow_admitted_allocation_order():
    card, _, _, _ = setup()
    card.memory.dram.free_list = [(1 << 20, 512 << 10), (2 << 20, 768 << 10)]
    log = [
        ("run", {0: (bytes([i]) * size, ())})
        for i, size in ((1, 256 << 10), (2, 512 << 10), (3, 512 << 10))
    ]
    with (
        patch.object(packages, "admit"),
        patch.object(packages, "Package", lambda *args: None),
    ):
        prepared = NodeCard.prepare_recorded(card, log, 2 << 20)
    assert prepared.selection["pinned_packages"] == 3
    assert held_bytes(card) == 1280 << 10
    prepared.close()


def multinode_witness():
    card, queue, _, _ = setup()
    other = NodeQueue(
        HostMemory(),
        0,
        sq_n=4,
        cq_n=4,
        spill=lambda size: card.memory.alloc(size, owner="package"),
        unspill=card.memory.free,
        record=LAYOUT.record,
        layout=LAYOUT,
    )
    other.init()
    card.queues[1] = other
    card.memory.dram.free_list = [(1 << 20, 1280 << 10), (3 << 20, 512 << 10)]
    hot, large, small = bytes(512 << 10), bytes(768 << 10), bytes([1]) * (512 << 10)
    log = [("run", {0: (hot, ())})] * 3 + [("run", {1: (small, ()), 0: (large, ())})]
    with (
        patch.object(packages, "admit"),
        patch.object(packages, "Package", lambda *args: None),
    ):
        prepared = NodeCard.prepare_recorded(card, log, 512 << 10, depth=1)
    try:
        first = card.memory.alloc(len(small), owner="stream")
        second = card.memory.alloc(len(large), owner="stream")
        card.memory.free(first)
        card.memory.free(second)
        assert not queue.has_prepared(hot)
    finally:
        prepared.close()


def test_multinode_stream_workspace_does_not_assume_queue_order():
    multinode_witness()
    assert "app.replay/split_workspace" in rs.mutants()
    with rs.mutant("app.replay/split_workspace"), pytest.raises(OutOfMemory):
        multinode_witness()


def test_list_metadata_charges_allocator_alignment():
    card, queue, _, log = setup()
    with (
        patch.object(packages, "admit"),
        patch.object(packages, "Package", lambda *args: None),
    ):
        prepared = NodeCard.prepare_recorded(card, log[:1] * 2, 448)
    events, _ = NodeCard.prepare_execution(card, prepared)
    assert events[0][0] == "run" and held_bytes(card) == 256
    prepared.close()
    # LIST descriptor size derives from layout.sq_bytes, not layout.line.
    with (
        patch.object(packages, "admit"),
        patch.object(packages, "Package", lambda *args: None),
    ):
        prepared = NodeCard.prepare_recorded(card, log[:1] * 2, 512)
    queue.layout = replace(LAYOUT, sq_bytes=256)
    events, _ = NodeCard.prepare_execution(card, prepared)
    assert events[0][0] == "run" and not prepared.execution_lists
    prepared.close()
