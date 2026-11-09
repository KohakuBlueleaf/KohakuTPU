"""Captured row kernels preserve cross-launch values through lifetime joins."""

import json
import struct

import numpy as np

from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.graph.capture import Capture, Library, Span, Template
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.split import SplitRunner
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.stack import rs


def bind_rows(graph, addresses):
    return NativeLaunch(graph, dict(zip(("in0", "out"), addresses, strict=True)), "dsl")


def retirement_frontiers(raw, header, steps):
    """Read the emitted engine WAITs, not BARRIER opcodes erased by lowering.

    This small straight-line witness never wraps a completion counter. Track
    each unit's issued instructions (including fetch batches) and mover GO,
    then require a WAIT for every issued completion at a quiescent frontier.
    """
    units = {}
    for index in range((header[A.KA_PH_COUNTS2] >> 32) & 65535):
        word = struct.unpack_from("<Q", raw, header[A.KA_PH_OFF_UNIT] + 16 * index)[0]
        units[(word & 255, (word >> 8) & 255)] = index
    sent, waited = {}, {}
    mailbox = [0] * 5
    before, frontiers = {}, set()

    def quiet():
        return bool(sent) and all(
            waited.get(src, 0) == count for src, count in sent.items()
        )

    for position, (word, arg) in enumerate(steps):
        before[position] = (quiet(), sum(sent.values()))
        op, flags = word & 255, (word >> 8) & 255
        if op != A.KA_OP_ENGINE:
            assert op in (A.KA_OP_RING, A.KA_OP_WAIT_SLOT, A.KA_OP_END)
            continue
        assert flags == 0, "the frontier proof requires synchronous engine execution"
        for index in range(word >> 32):
            group = struct.unpack_from(
                "<4Q", raw, header[A.KA_PH_OFF_PAY] + 32 * (arg + index // 3)
            )
            code = (group[0] >> (8 * (index % 3))) & A.KOS_DE_CODE_MASK
            value = group[index % 3 + 1]
            if code == A.KOS_DE_WAIT_CODE:
                waited[value >> 56] = value & A.KOS_DE_CTR_MASK
                if quiet():
                    frontiers.add(tuple(sorted(sent.items())))
            elif code < A.KOS_DE_MB_GO:
                mailbox[code] = value
            elif code == A.KOS_DE_MB_GO:
                fetch = mailbox[A.KOS_DE_MB_DST] >= A.KOS_DE_FETCH_TYPED
                coord = (
                    mailbox[A.KOS_DE_MB_ARG2] >> 40
                    if fetch
                    else (
                        (mailbox[A.KOS_DE_MB_DST] >> 4) & 0xF0
                        | mailbox[A.KOS_DE_MB_DST] & 15
                    )
                )
                src = units[(coord & 15, coord >> 4)]
                amount = mailbox[A.KOS_DE_MB_ARG3] & 255 if fetch else 1
                sent[src] = sent.get(src, 0) + amount
            elif code == A.KOS_DE_MV_CTRL and value & A.KOS_MV_CTRL_GO:
                src = A.KOS_DE_MOVER_SRC
                sent[src] = sent.get(src, 0) + 1
    assert sent and max(sent.values()) <= A.KOS_DE_CTR_MASK
    assert quiet(), "final engine close must retire all issued work"
    return before, frontiers, sum(sent.values())


def assert_launch_join(prepared, artifact, nodes):
    """Check the emitted protocol, not just a self-reported trace or graph shape.

    Private storage is pooled across launches, so producer-only row handshakes
    have been superseded by a whole-launch close plus gather/release. No direct
    memory-producer edge may be invented for the coordinator's release ring.
    """
    joins = [e for e in prepared.events if e["kind"] == "lifetime_join"]
    assert joins
    assert all(e["after_launch"] == 0 for e in joins)
    assert all(e["scope"] == "all_prior_work" and e["regions"] == [] for e in joins)
    # Flat event records remain usable by slot and memory-region observers.
    assert all(isinstance(e["regions"], list) for e in prepared.events)
    assert all(e["slot"] is None or 9 <= e["slot"] < 16 for e in prepared.events)
    coordinator = min(nodes)
    peers = sorted(set(nodes) - {coordinator})
    expected = (
        {(coordinator, coordinator, "local")}
        if not peers
        else {(n, coordinator, "gather") for n in peers}
        | {(coordinator, n, "release") for n in peers}
    )
    assert len(joins) == len(expected)
    assert {(e["producer"], e["consumer"], e["join_phase"]) for e in joins} == expected
    slots = {e["slot"] for e in joins}
    assert len(slots) == 1
    slot = slots.pop()
    if peers:
        assert slot is not None and 9 <= slot < 16
        assert {(n, slot) for n in nodes} <= artifact.slot_leases
    else:
        assert slot is None
    for part in artifact.parts:
        header = struct.unpack_from(A.PKG_HDR_FMT, part.package)
        steps = [
            struct.unpack_from("<QQ", part.package, header[A.KA_PH_OFF_STEP] + 16 * i)
            for i in range((header[A.KA_PH_COUNTS2] >> 16) & 65535)
        ]
        before, frontiers, total = retirement_frontiers(part.package, header, steps)
        if not peers:
            # P1 has no slot boundary in the wire format. Check nonterminal
            # retirement too; the changed-value replays below check data order.
            assert len(frontiers) >= 2
            continue
        protocol, positions = [], []
        for index, (word, arg) in enumerate(steps):
            op, unit, count = word & 255, (word >> 16) & 65535, word >> 32
            flags = (word >> 8) & 255
            if op == A.KA_OP_RING and count == slot:
                assert flags == 0 and arg >> 16 == 1
                protocol.append(("ring", unit, (arg & 65535) or 1))
            elif op == A.KA_OP_WAIT_SLOT and unit == slot:
                assert flags == A.KA_STEP_F_CONSUME and arg == 0
                protocol.append(("wait", part.node, count))
            else:
                continue
            positions.append(index)
        assert protocol == (
            [("wait", coordinator, len(peers)), *(("ring", n, 1) for n in peers)]
            if part.node == coordinator
            else [("ring", coordinator, 1), ("wait", part.node, 1)]
        )
        # Arrival/release cannot authorize reuse while the old work is live;
        # every participant must also execute new work after the release.
        assert all(before[position][0] for position in positions)
        assert 0 < before[positions[0]][1] == before[positions[-1]][1] < total


def test_dependent_split_rows_use_only_their_producers(monkeypatch):
    # "Only their producers" remains a value contract. Scheduling now waits
    # for whole-launch quiescence as well, because the next launch reuses scratch.
    rows, width = 64, 32
    values = (np.arange(rows * width) % 97 - 48).astype(np.float16).reshape(rows, width)
    raw = values.tobytes()
    outputs = []
    for count in (1, 4):
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=count)
        prepared = None
        try:
            addresses = {name: dev.alloc(len(raw)) for name in ("x", "y", "z")}
            dev.write(addresses["x"], raw)
            capture = Capture()
            grants = [tuple(dev.card.nodes)]
            grants.append(grants[0][1:] + grants[0][:1])
            operand = {
                "kind": "full",
                "dtype": "f16",
                "offset": 0,
                "pitch": width,
                "rows": rows,
            }
            for index, (operation, value) in enumerate((("mul", 0.5), ("add", 1.0))):
                arguments = {
                    "e": {
                        "nodes": [
                            {"load": 0},
                            {"const": value},
                            {"op": operation, "args": [0, 1]},
                        ],
                        "root": 2,
                    },
                    "operands": [operand],
                    "R": rows,
                    "W": width,
                    "out": operand,
                }
                source, selection = generated.split_graph(
                    "vector.rows_kernel",
                    json.dumps(arguments),
                    rs.machine_json(dev.machine),
                    None,
                    grants[index],
                    None,
                )
                assert selection["active_nodes"] == count
                graph = json.loads(source)
                src, dst = (("x", "y"), ("y", "z"))[index]
                capture.launch(
                    Template((operation, count), "elementwise", bind_rows),
                    graph,
                    (Span(src, 0, len(raw)),),
                    Span(dst, 0, len(raw)),
                    addresses,
                    lambda packet, dev=dev: dev.run(
                        dev.kernel(json.dumps(packet.graph)), packet.bindings
                    ),
                )
            eager = dev.read(addresses["z"], len(raw))
            semantic = capture.finish((Span("z", 0, len(raw)),), Library())
            prepared = semantic.prepare(SplitRunner(dev), addresses)
            artifact = prepared.bind_artifact()
            assert_launch_join(prepared, artifact, dev.card.nodes)
            pending = []
            submit = dev.card.submit_launch

            def published(*args, submit=submit, pending=pending):
                result = submit(*args)
                pending.append(result)
                return result

            monkeypatch.setattr(dev.card, "submit_launch", published)
            prepared.submit()
            assert dev.read(addresses["z"], len(raw)) == eager
            replayed = []
            for increment in (0, 7, 19):
                current = values + np.float16(increment)
                dev.write(addresses["x"], current.tobytes())
                dev.write(addresses["y"], bytes([0xFF]) * len(raw))
                dev.write(addresses["z"], bytes([0xFF]) * len(raw))
                prepared.submit()
                result = dev.read(addresses["z"], len(raw))
                expected = (current * np.float16(0.5) + np.float16(1.0)).tobytes()
                assert result == expected
                if increment == 0:
                    assert result == eager
                assert prepared.bind_artifact() is artifact
                replayed.append(result)
            outputs.append(replayed)
            assert len(pending) == 4
            for invocation in pending:
                assert invocation.order == [
                    *(("publish", n) for n in dev.card.nodes),
                    *(("collect", n) for n in dev.card.nodes),
                ]
        finally:
            if prepared is not None:
                prepared.close()
            dev.close()
    assert outputs[1] == outputs[0]
