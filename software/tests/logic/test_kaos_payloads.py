"""Bound retained ENGINE payloads cost their fetches, not prior launches.

These already-lowered composed KAPK fixtures use cumulative ENGINE waits and
real gather/release slots. The manifest selects the multi-launch layout; it
never certifies an execution edge. No card or native compiler is needed.
"""

import json
import struct

import pytest

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart

SLOT = A.KOS_SLOT_LIMIT - 1
WORDS = 128


def step(op, unit=0, count=0, arg=0, flags=0):
    return op | flags << 8 | unit << 16 | count << 32, arg


def groups(entries):
    out = bytearray()
    for at in range(0, len(entries), 3):
        chunk = entries[at : at + 3]
        codes = sum(code << (8 * j) for j, (code, _) in enumerate(chunk))
        values = [value for _, value in chunk]
        out.extend(struct.pack("<4Q", codes, *(values + [0] * (3 - len(values)))))
    return bytes(out)


def words(label, count=WORDS):
    return b"".join(struct.pack("<4Q", label, i, label ^ i, 0) for i in range(count))


def part(node, nodes, labels, windows=((0, WORDS),), descriptor_xor=0):
    # Engine lowering starts with every original payload, then appends groups.
    payload = bytearray(b"".join(words(label) for label in labels))
    steps, expected = [], []
    sent = 0
    for launch, label in enumerate(labels):
        entries = []
        for offset, count in windows:
            sent += count
            entries.extend(
                [
                    (A.KOS_DE_MB_DST, A.KOS_DE_FETCH_TYPED | 3),
                    (A.KOS_DE_MB_ARG0, 0),
                    (A.KOS_DE_MB_ARG1, 0),
                    (A.KOS_DE_MB_ARG2, A.de_fetch_arg2(1, 2)),
                    (
                        A.KOS_DE_FETCH_CODE,
                        A.de_fetch_arg3(32 * (launch * WORDS + offset), count)
                        ^ descriptor_xor,
                    ),
                    (A.KOS_DE_MB_GO, 1),
                    (A.KOS_DE_WAIT_CODE, sent & A.KOS_DE_CTR_MASK),
                ]
            )
            expected.append(words(label)[32 * offset : 32 * (offset + count)])
        steps.append(step(A.KA_OP_ENGINE, count=len(entries), arg=len(payload) // 32))
        payload.extend(groups(entries))
        if launch + 1 < len(labels) and nodes > 1:
            wait = step(
                A.KA_OP_WAIT_SLOT,
                unit=SLOT,
                count=nodes - 1 if node == 0 else 1,
                flags=A.KA_STEP_F_CONSUME,
            )
            if node == 0:
                steps.append(wait)
                steps.extend(
                    step(A.KA_OP_RING, other, SLOT, 1 | 1 << 16)
                    for other in range(1, nodes)
                )
            else:
                steps.extend([step(A.KA_OP_RING, 0, SLOT, 1 | 1 << 16), wait])
    steps.append(step(A.KA_OP_END))
    header = [0] * 12
    header[A.KA_PH_MAGIC] = 96 << 48 | A.KA_PKG_VERSION << 32 | A.KA_PKG_MAGIC
    header[A.KA_PH_OFF_UNIT] = 96
    header[A.KA_PH_OFF_STEP] = 112
    header[A.KA_PH_OFF_PAY] = 112 + 16 * len(steps)
    header[A.KA_PH_SIZE] = header[A.KA_PH_OFF_PAY] + len(payload)
    header[A.KA_PH_COUNTS] = len(payload) // 32
    header[A.KA_PH_COUNTS2] = len(steps) << 16 | 1 << 32
    unit = int.from_bytes(b"VC", "big") << 32 | node << 16 | 2 << 8 | 1
    raw = (
        struct.pack(A.PKG_HDR_FMT, *header)
        + struct.pack("<2Q", unit, 512 | (1 << 16 | 3) << 32)
        + b"".join(struct.pack("<2Q", *s) for s in steps)
        + payload
    )
    return NodePart(node, raw), expected


def artifact(nodes, labels, **options):
    parts = [part(node, nodes, labels, **options) for node in range(nodes)]
    manifest = json.dumps({"dependencies": [[] for _ in labels]}).encode()
    return (
        BoundGraphArtifact(
            "payload-witness", tuple(p for p, _ in parts), manifest=manifest
        ),
        parts[0][1],
    )


def translate(bound):
    return Graph.package_parts(
        bound, {(p.node, 1, 2): p.node for p in bound.parts}, wait_window=True
    )


def fetched(graph, base_ref=0x0100000000010000):
    """Resolve upload pointers and descriptors, not just host-side blob lengths."""
    raw = graph.pack(base_ref)
    result = []
    for i, task in enumerate(graph.tasks):
        if task.kind not in (A.KDS_DE_FEED, A.KDS_DE_STREAM):
            continue
        ref, count, size, _ = struct.unpack_from(
            "<4Q", raw, i * A.OSTPU_TASK_BYTES + A.KDS_TASK_OFF_WORDS
        )
        at = ref - base_ref
        end = at + A.de_cmd_bytes(count) + size
        assert len(graph.tasks) * A.OSTPU_TASK_BYTES <= at < end <= len(raw)
        for k in range(count):
            group = struct.unpack_from("<4Q", raw, at + 32 * (k // 3))
            code, value = (group[0] >> (8 * (k % 3))) & 255, group[k % 3 + 1]
            if code == A.KOS_DE_FETCH_CODE:
                n = value & 255
                assert (
                    value & ((1 << A.KOS_DE_FETCH_SHIFT) - 1) == A.KOS_DE_FETCH_TAG | n
                )
                start = at + (value >> A.KOS_DE_FETCH_SHIFT)
                assert at + A.de_cmd_bytes(count) <= start < start + 32 * n <= end
                result.append(raw[start : start + 32 * n])
    return result


@pytest.mark.parametrize("nodes", [2, 4])
@pytest.mark.parametrize("launches", [4, 16])
def test_composed_retained_bytes_are_launch_bytes_plus_joins(nodes, launches):
    labels = tuple(range(1, launches + 1))
    bound, expected = artifact(nodes, labels)
    graphs = translate(bound)
    singles = [translate(artifact(nodes, (label,))[0]) for label in labels]
    for node, graph in graphs.items():
        assert fetched(graph) == expected
        launch_bytes = sum(len(single[node].pack()) for single in singles)
        # Acquire plus (empty-feed NOP, drain, release) per peer; group padding.
        join_bytes = (launches - 1) * ((1 + 3 * (nodes - 1)) * A.OSTPU_TASK_BYTES + 64)
        assert len(graph.pack()) <= launch_bytes + join_bytes


@pytest.mark.parametrize("limit", [6, 7, 8])
def test_fetch_bytes_survive_a_capacity_cut_before_or_after_arg3(monkeypatch, limit):
    # With one unit reserve=2: limits 6/7 cut before/after ARG3; 8 after GO.
    monkeypatch.setattr(A, "KOS_DE_MAX_ENTRIES", limit)
    bound, expected = artifact(2, (3, 17, 29))
    for graph in translate(bound).values():
        assert fetched(graph) == expected
        blob_bytes = sum(t.words[2] for t in graph.tasks if t.kind == A.KDS_DE_FEED)
        assert blob_bytes == len(expected) * WORDS * 32


def test_composed_nonzero_and_overlapping_fetches_attach_only_requested_ranges():
    windows = ((11, 23),) * 5 + ((7, 60), (23, 3))
    bound, expected = artifact(2, (5, 41), windows=windows)
    for graph in translate(bound).values():
        assert fetched(graph) == expected
        # Identical requests share bytes; unequal overlapping ranges are distinct.
        blob_bytes = sum(t.words[2] for t in graph.tasks if t.kind == A.KDS_DE_FEED)
        assert blob_bytes == 2 * (23 + 60 + 3) * 32


@pytest.mark.parametrize(
    "offset,count,descriptor_xor",
    [(1 << 20, 1, 0), (0, 0, 0), (2 * WORDS - 1, 255, 0), (0, 1, 1 << 13)],
)
def test_bad_relative_fetch_refuses_instead_of_slicing_short(
    offset, count, descriptor_xor
):
    # Composed-only validation: ordinary translation keeps its base verdict.
    bound, _ = artifact(
        2, (1, 2), windows=((offset, count),), descriptor_xor=descriptor_xor
    )
    reason = "descriptor" if descriptor_xor else "outside package payload"
    with pytest.raises(ValueError, match=reason):
        translate(bound)
