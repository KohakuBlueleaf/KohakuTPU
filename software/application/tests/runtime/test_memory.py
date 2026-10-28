"""The card's memory has one owner (`runtime.memory.CardMemory`): arrays, a
recording's carved spans and packages spilled past a queue's heap never share
an address, and painting never writes over a live span. Device-free: the node
queue runs against the spec firmware over an in-memory card."""

import json
from itertools import pairwise

import pytest

from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakuaccel.simulation.package import Interpreter
from kohakutpu.application.runtime.arena import Arena, OutOfMemory
from kohakutpu.application.runtime.card import ARENA, Q_SIZE, node_queue
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF
from kohakutpu.simulation.package import Reader

QUEUE = 0x8000_0000
STAGE = (QUEUE + Q_SIZE, QUEUE + (2 << 20))


class Quiet:
    """A mailbox nothing is sent to."""

    def send(self, x, y, payload):
        raise AssertionError("no unit is addressed")

    def drain(self):
        return []


def pattern(seed: int, nbytes: int) -> bytes:
    return bytes((seed * 131 + k * 7) & 0xFF for k in range(nbytes))


def test_alloc_top_takes_the_top_of_the_highest_span_and_gives_it_back():
    a = Arena(0x1000, 0x1000 + 10 * 256 + 100)
    low = a.alloc(256)
    top = a.alloc_top(512)
    assert low == 0x1000 and top == 0x1000 + 8 * 256
    assert a.alloc(7 * 256) == 0x1000 + 256
    with pytest.raises(OutOfMemory):
        a.alloc(256)
    a.release(top)
    a.release(low)
    assert a.alloc_top(256) == 0x1000 + 9 * 256


def _exclusive_lines(arena, line):
    spans = sorted(arena.live.items())
    return all(at % line == 0 for at, _ in spans) and all(
        (a + n - 1) // line < b // line for (a, n), (b, _) in pairwise(spans)
    )


def test_runtime_allocations_reserve_exclusive_machine_lines():
    for name in MF.names():
        line = MF.load(name).card.dram.line_bytes
        mem = CardMemory((ARENA + 1, ARENA + (1 << 20)), {0: STAGE})
        for n in (1, 32, 33, 64, 1568):
            mem.alloc(n)
            mem.stage(n, 0)
        carved = mem.carve(8192)
        for n in (1, 32, 33):
            carved.alloc(n)
        assert all(_exclusive_lines(a, line) for a in (*mem.arenas(), carved))
        assert all(a.align % line == 0 for a in (*mem.arenas(), carved))
    word_aligned = Arena(ARENA, ARENA + 8192, align=32)
    word_aligned.alloc(32)
    word_aligned.alloc(32)
    assert not _exclusive_lines(word_aligned, 64)


def test_device_reserves_and_exposes_whole_write_lines():
    dev = Device(kind="offline", board="multimesh_v9t6")
    line = MF.load(dev.machine.board).get("card.dram.line_bytes")
    for n in (1, 32, 33, 64, 65, 1568):
        at = dev.alloc(n)
        entry = dev.mmu.entries[at]
        assert entry.physical % line == 0
        assert entry.extent == max(line, -(-n // line) * line)
        assert entry.extent <= entry.arena.live[entry.physical]
        assert entry.arena.requested[entry.physical] == n
        dev.free(at)
    dev.line_bytes = MF.load(dev.machine.board).die.word_bytes
    at = dev.alloc(32)
    assert dev.mmu.entries[at].extent < line
    dev.free(at)


def test_painting_never_writes_a_live_span():
    writes: list = []
    mem = CardMemory(
        (ARENA, ARENA + (1 << 20)), {}, paint=lambda at, b: writes.append((at, len(b)))
    )
    held: dict = {}

    def checked(op):
        before = dict(mem.dram.live)
        n = len(writes)
        at = op()
        for w, k in writes[n:]:
            for a, size in before.items():
                assert w + k <= a or a + size <= w, (hex(w), k, hex(a), size)
        return at

    held["a"] = checked(lambda: mem.alloc(5000))
    private = checked(lambda: mem.carve(64 << 10))
    assert writes[-1] == (private.lo, 64 << 10)  # a carved span is zeroed whole
    held["b"] = checked(lambda: mem.alloc(3000))
    mem.free(held.pop("a"))
    held["c"] = checked(lambda: mem.alloc(9000))
    held["d"] = checked(lambda: mem.alloc(200))
    assert all(private.lo >= at + mem.dram.live[at] for at in held.values())


def test_mark_and_release_free_what_was_placed_since():
    mem = CardMemory((ARENA, ARENA + (1 << 20)), {0: STAGE})
    keep = mem.alloc(1000)
    mark = mem.mark()
    mem.alloc(2000)
    mem.stage(512, 0)
    mem.release(mark)
    assert list(mem.dram.live) == [keep] and not mem.staging[0].live


def test_a_spilled_package_never_lands_on_an_array():
    host = MemoryTransport()
    memory = CardMemory((ARENA, ARENA + (8 << 20)), {0: STAGE})
    fw = SpecFirmware(host, QUEUE, Interpreter(Quiet(), reader=Reader()))
    q = node_queue(host, QUEUE, memory, idle=fw.step)
    q.init()
    fw.boot()

    arrays = {}
    for k in range(3):
        at = memory.alloc(64 << 10)
        arrays[at] = pattern(k, 64 << 10)
        host.write_block(at, arrays[at])
    private = memory.carve(1 << 20)
    rec = private.alloc(4096)
    arrays[rec] = pattern(9, 4096)
    host.write_block(rec, arrays[rec])

    words = (Q_SIZE - q.heap_off) // 32 + 64
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    big = _rs.package_build(
        '{"credit": 32}',
        json.dumps([["payload", 0xC0DE << 40 | k] for k in range(words)]),
        text,
    )
    assert len(big) > Q_SIZE - q.heap_off
    assert q.run(big).ok
    for at, want in arrays.items():
        assert host.read_block(at, len(want)) == want, hex(at)

    # Arrays placed after the spill do not land on it: the package still runs
    # (a corrupted one fails to parse).
    for k in range(2):
        at = memory.alloc(256 << 10)
        arrays[at] = pattern(20 + k, 256 << 10)
        host.write_block(at, arrays[at])
    assert q.run(big).ok and q.counters["uploads"] == 2
    for at, want in arrays.items():
        assert host.read_block(at, len(want)) == want, hex(at)

    used = memory.dram.used
    q.drop_spilled()
    assert memory.dram.used == used and not q._spilled
