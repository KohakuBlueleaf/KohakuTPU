"""A package's AWAITs through the OS lowering, played on a double of the dispatch
engine: counters cleared per stream, a word retired at its GO, a WAIT held until its
counter reaches it, so a want no completion can reach is a hang, told here."""

import struct

import pytest

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    DATA_ALIGN,
    Graph,
    ResidentGraph,
    mover_code,
    task_capacity,
)

VC = int.from_bytes(b"VC", "big")
COORD = [(1, 2), (2, 2)]
UNITS = {(0, *c): i for i, c in enumerate(COORD)}


def step(op, unit=0, count=0, arg=0, flags=0):
    return op | flags << 8 | unit << 16 | count << 32, arg


def package(steps, fetch=0):
    """KAPK v2 bytes (package-format.md s2): the unit table, the steps, then enough
    payloads for every DISPATCH, each word distinct and in `arg` order."""
    header = struct.calcsize(A.PKG_HDR_FMT)
    unit_at, step_at = header, header + 16 * len(COORD)
    pay_at = step_at + 16 * len(steps)
    npay = max(
        (arg + (a >> 32) for a, arg in steps if a & 255 == A.KA_OP_DISPATCH), default=0
    )
    h = [0] * (header // 8)
    h[A.KA_PH_MAGIC] = header << 48 | A.KA_PKG_VERSION << 32 | A.KA_PKG_MAGIC
    h[A.KA_PH_SIZE] = pay_at + 32 * npay
    h[A.KA_PH_COUNTS] = npay
    h[A.KA_PH_COUNTS2] = len(steps) << 16 | len(COORD) << 32
    h[A.KA_PH_OFF_UNIT], h[A.KA_PH_OFF_STEP] = unit_at, step_at
    h[A.KA_PH_OFF_PAY] = pay_at
    raw = struct.pack(A.PKG_HDR_FMT, *h)
    for x, y in COORD:
        raw += struct.pack("<2Q", VC << 32 | y << 8 | x, 512 | fetch << 32)
    raw += b"".join(struct.pack("<2Q", *s) for s in steps)
    return raw + b"".join(struct.pack("<4Q", i + 1, 2, 3, 4) for i in range(npay))


def entries(graph, task):
    count = graph.tasks[task].words[1]
    raw = graph.streams[task]
    out = []
    for k in range(count):
        group = struct.unpack_from("<4Q", raw, 32 * (k // 3))
        out.append(((group[0] >> (8 * (k % 3))) & 255, group[k % 3 + 1]))
    return out


def play(graph, peers=()):
    """Every stream in order. `peers` is (word count of unit a, unit b): the word that
    makes b's peer retire once, without the node having sent b anything."""
    waits = {}
    sent_of = {}
    for task in sorted(graph.streams):
        count, dst, moves = {}, 0, 0
        for code, value in entries(graph, task):
            if code == A.KOS_DE_WAIT_CODE:
                src, want = value >> 56, value & A.KOS_DE_CTR_MASK
                have = moves if src == A.KOS_DE_MOVER_SRC else count.get(src, 0)
                # dispatch_engine.v: `behind`'s top bit holds the WAIT.
                held = (have - want) & A.KOS_DE_CTR_MASK > A.KOS_DE_CTR_MASK >> 1
                assert not held, f"WAIT({src}, {want}) holds at {have}: a hang"
                waits.setdefault(src, []).append(want)
            elif code == A.KOS_DE_MB_DST:
                dst = value
            elif code == A.KOS_DE_MB_GO:
                unit = COORD.index((dst & 255, dst >> 8 & 255))
                count[unit] = count.get(unit, 0) + 1
                sent_of[unit] = sent_of.get(unit, 0) + 1
                for by, at, other in peers:
                    if by == unit and at == sent_of[unit]:
                        count[other] = count.get(other, 0) + 1
            elif code == mover_code(0) and value & A.KOS_MV_CTRL_GO:
                moves += 1
    return waits


def lowered(*packages, fuse=False):
    g = Graph(compact=True, movers=True, de_stream=True, de_fetch=False, de_fuse=fuse)
    front = ()
    for raw in packages:
        front = g.package(raw, [], UNITS, after=front)
    return g


D, AWAIT, BARRIER = A.KA_OP_DISPATCH, A.KA_OP_AWAIT, A.KA_OP_BARRIER


def barrier_then_await():
    """LeNet's shape: one unit awaited, a barrier, then the other unit's AWAIT."""
    return package(
        [
            step(D, 0, 3, 0),
            step(D, 1, 2, 3),
            step(AWAIT, 0, 3),
            step(BARRIER),
            step(AWAIT, 1, 2),
            step(BARRIER),
            step(A.KA_OP_END),
        ]
    )


def test_an_await_after_a_barrier_is_the_words_sent_not_twice_them():
    assert play(lowered(barrier_then_await())) == {0: [3, 3, 3, 3], 1: [2, 2, 2, 2]}


def test_a_fused_stream_counts_on_from_the_package_before():
    waits = play(lowered(barrier_then_await(), barrier_then_await(), fuse=True))
    assert waits == {0: [3, 6], 1: [2, 4]}


def test_a_wait_past_the_words_sent_is_a_peers_retirement():
    raw = package(
        [
            step(D, 0, 1, 0),
            step(D, 1, 1, 1),
            step(AWAIT, 0, 2),
            step(A.KA_OP_END),
        ]
    )
    assert play(lowered(raw), peers=[(1, 1, 0)])[0] == [2, 2]
    with pytest.raises(AssertionError, match="a hang"):
        play(lowered(raw))


def test_a_stream_split_counts_what_the_stream_before_it_waited_for():
    words = A.KOS_DE_MAX_ENTRIES // 2
    raw = package(
        [
            step(D, 0, 1, 0),
            step(D, 1, 1, 1),
            step(AWAIT, 0, 2),
            step(D, 1, words, 2),
            step(D, 0, 1, 2 + words),
            step(AWAIT, 0, 1),
            step(A.KA_OP_END),
        ]
    )
    g = lowered(raw)
    assert len(g.streams) == 2
    assert play(g, peers=[(1, 1, 0)])[0] == [2, 2, 1, 1]


def test_task_records_end_on_a_write_burst_line():
    for tasks in range(1, 600):
        capacity = task_capacity(tasks)
        assert tasks <= capacity < tasks + DATA_ALIGN // A.OSTPU_TASK_BYTES
        assert capacity * A.OSTPU_TASK_BYTES % DATA_ALIGN == 0


class Mailbox:
    """A booted `ostpu`'s mailbox, read through the session's transport."""

    def __init__(self, data_pa):
        self.data_pa = data_pa

    def read(self, pa, size):
        words = (0, 0, 0, 0)
        if pa == A.OSTPU_OFF_READY:
            words = (A.OSTPU_MAGIC, 0x1000, self.data_pa, 0)
        return struct.pack("<4Q", *words).ljust(size, b"\0")[:size]


def test_a_session_refuses_a_data_arena_off_a_write_burst_line():
    assert ResidentGraph(Mailbox(0x1000 + DATA_ALIGN), 0).data_pa == 0x1100
    with pytest.raises(ValueError, match="task_capacity"):
        ResidentGraph(Mailbox(0x1000 + DATA_ALIGN // 2), 0)
