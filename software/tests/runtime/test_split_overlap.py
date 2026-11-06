"""Admission orders split hazards without collecting unrelated queues."""

import json
import time

import pytest

from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime.device.common import Submitted
from kohakutpu.application.runtime.graph.leases import package_slots
from kohakutpu.stack.rs import _rs


def test_split_fence_orders_lines_and_shared_scratch_only(monkeypatch):
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    try:
        monkeypatch.setattr(dev.card, "collect", lambda pending: {"cycles": 0})
        address = dev.alloc(256)
        entry, physical = dev.mmu.resolve(address)
        ref = (entry.arena, entry.physical)
        line = physical // dev.line_bytes

        def held(lo, hi, write, nodes):
            references = dev._retain([address])
            pending = Submitted(
                "held",
                "dsl",
                {},
                time.monotonic(),
                references=references,
                writes=references.copy() if write else [],
                nodes=nodes,
                accesses=((entry.arena, line + lo, line + hi, write),),
            )
            dev.inflight.append(pending)
            return pending

        unrelated = held(2, 3, True, (0, 1))
        for previous_write, next_write in ((True, False), (False, True), (True, True)):
            hazard = held(0, 1, previous_write, (2, 3))
            dev._fence(
                (0, 1, 2, 3),
                [ref],
                [ref] if next_write else [],
                ((entry.arena, line, line + 1, next_write),),
            )
            assert hazard.result is not None
            assert hazard.run is None
            assert unrelated.result is None
        reader = held(0, 1, False, (2, 3))
        dev._fence((0, 1), [ref], [], ((entry.arena, line, line + 1, False),))
        assert reader.result is None
        dev.drain()

        window = dev._windowed([("scratch", 64, "dram", None, ())])["scratch"]
        scratch_refs = dev._retain([window])
        pending = Submitted(
            "scratch",
            "dsl",
            {},
            time.monotonic(),
            references=scratch_refs,
            writes=scratch_refs.copy(),
            nodes=(0, 1),
        )
        dev.inflight.append(pending)
        assert dev._windowed([("scratch", 64, "dram", None, ())])["scratch"] == window
        dev._fence((2, 3), scratch_refs, scratch_refs)
        assert pending.run is not None
    finally:
        dev.close()


def test_native_split_submission_uses_the_same_overlap_policy(monkeypatch):
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    dev = card.Device(kind="offline", board="multimesh_v9t8", nodes=4, queue_depth=8)
    try:
        machine = dev.card.machine_json
        transform = json.loads(machine)["machine"]["mover"]["modes"]["transform"]
        raw = _rs.package_build(
            '{"credit":32}',
            json.dumps(
                [
                    ["spans_from", [[0, 32]]],
                    [
                        "mover",
                        [[0x10, 0], [0, (1 << 16) | transform]],
                        {"addresses": [[[68, 40, 0]]]},
                    ],
                    ["end"],
                ]
            ),
            machine,
        )
        scope = (dev.recipe_epoch, dev.recipe_epoch.generation)
        base = dev.alloc(256)
        handles = [base + offset for offset in range(0, 256, 64)]
        nodes = tuple(dev.card.nodes)

        def submit(group, addresses):
            packages = [
                (node, _rs.LaunchRecipe(raw, machine, mesh=node, scope=scope), [i])
                for i, node in enumerate(group)
            ]
            native = _rs.NativeSubmit(
                "disjoint",
                "dsl",
                {},
                [],
                [(0, i, 0) for i in range(len(group))],
                packages,
                False,
                scope,
                accesses=[(32, True)] * len(group),
            )
            return native.launch(dev, addresses)

        first = submit(nodes[:2], handles[:2])
        with monkeypatch.context() as patch:
            patch.setattr(dev, "drain", lambda: pytest.fail("unrelated run drained"))
            second = submit(nodes[2:], handles[2:])
            assert list(dev.inflight) == [first, second]
            assert first.run is None
    finally:
        dev.close()


def test_split_admission_orders_shared_receiver_slots_only(monkeypatch):
    dev = card.Device(kind="double", board="multimesh_v9t8", nodes=4)
    observed = []

    def collect(pending):
        observed.append(pending)
        return {"cycles": 0}

    monkeypatch.setattr(dev.card, "collect", collect)
    try:

        def slots(receiver, slot):
            raw = _rs.package_build(
                '{"credit":32}',
                json.dumps([["ring", receiver, {"slot": slot}], ["end"]]),
                dev.card.machine_json,
            )
            return package_slots(raw, dev.card.machine_json, 0)

        for label, resources in (
            ("other_receiver", slots(3, 9)),
            ("other_slot", slots(2, 10)),
            ("shared", slots(2, 9)),
        ):
            dev.inflight.append(
                Submitted(
                    label,
                    "graph",
                    {"slot_leases": resources},
                    time.monotonic(),
                    pending=label,
                    nodes=(0, 1, 2, 3),
                    accesses=(),
                )
            )
        dev._fence((1, 2), [], [], (), slots(2, 9))
        assert observed == ["shared"]
        assert all(pending.run is None for pending in dev.inflight)
    finally:
        dev.close()


def test_native_recipe_owns_receiver_slots_without_caller_metadata(monkeypatch):
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    dev = card.Device(kind="offline", board="multimesh_v9t8", nodes=4)
    observed = []
    try:
        machine = dev.card.machine_json
        scope = (dev.recipe_epoch, dev.recipe_epoch.generation)

        def launch(source, receiver, slot):
            calls = (
                [["ring", receiver, {"slot": slot}], ["end"]],
                [["wait_slot", slot, 1, {"consume": True}], ["end"]],
            )
            packages = [
                (
                    node,
                    _rs.LaunchRecipe(
                        _rs.package_build('{"credit":32}', json.dumps(body), machine),
                        machine,
                        mesh=node,
                        scope=scope,
                    ),
                    [],
                )
                for node, body in zip((source, receiver), calls, strict=True)
            ]
            native = _rs.NativeSubmit(
                "signals", "graph", {}, [], [], packages, False, scope
            )
            return native.launch(dev, [])

        def collect(pending):
            observed.append(pending)
            return {"cycles": 0}

        monkeypatch.setattr(dev.card, "collect", collect, raising=False)
        first = launch(0, 2, 9)
        first.result = None
        first.pending = "first"
        second = launch(1, 3, 9)
        second.result = None
        second.pending = "second"
        assert not observed
        launch(0, 2, 9)
        assert observed == ["first"]
        assert second.run is None
    finally:
        dev.close()
