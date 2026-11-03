"""What the card layer reads for a measurement: each die's interlink counters around
a run, the measure image's ring, and each sysnode's boot flags."""

import struct
from types import SimpleNamespace as NS

import pytest

from kohakutpu.application.runtime.card import interlink, measure
from kohakutpu.application.runtime.card.common import parse_boot_flags

CTRLS = [0x10_0000, 0x20_0000]
M32 = (1 << 32) - 1


class Link:
    """Each die's sixteen AUX_STATW words behind its control window; a write to
    the interlink control register sets the enable and clears the faults."""

    def __init__(self, narrow: bool):
        self.words = {c: [0] * 16 for c in CTRLS}
        self.writes = []
        if narrow:
            self.control_bytes = 4

    def read64(self, addr):
        c = addr & ~0xFFF
        return self.words[c][(addr - c - 0x80) // 8]

    def _write(self, addr, value, bits):
        c = addr & ~0xFFF
        assert addr - c == 0x880
        self.writes.append((c, value, bits))
        if value & 4:
            self.words[c][1] = 0
        self.words[c][0] = 0x1_2_494C if value & 1 else 0

    def write64(self, addr, value):
        self._write(addr, value, 64)

    def write32(self, addr, value):
        self._write(addr, value, 32)


@pytest.mark.parametrize("narrow", [False, True])
def test_counters_read_as_wrapped_deltas_around_a_run(narrow):
    t = Link(narrow)
    die0, die1 = (t.words[c] for c in CTRLS)
    die0[0] = 0x1_2_494C  # enabled
    die0[1] = 1 << 5  # a stale RING fault, cleared before the run
    die0[3] = 7 << 32 | M32 - 1  # last tag 7, 2^32 - 2 bells from mesh 1
    die0[6] = 10 << 32 | M32  # link 0 tx: 10 beats, 2^32 - 1 packets
    die0[13] = 0x44 << 32 | 0x33  # credits
    die1[0] = 0  # disabled

    def run():
        die0[3] = 9 << 32 | 2  # four more bells, last tag 9
        die0[6] = 46 << 32 | 3  # 36 beats in 4 packets
        die0[1] = 1 << 0 | 1 << 6  # RD_REMOTE and CW_OFF during the run
        die1[14] = 5
        return "ran"

    got, d = interlink.around(t, CTRLS, run)

    assert got == "ran"
    assert d[0]["bells_from1"] == 4 and d[0]["tag_from1"] == 9
    assert (d[0]["tx0_packets"], d[0]["tx0_beats"]) == (4, 36)
    assert (d[0]["credits0"], d[0]["credits1"]) == (0x33, 0x44)
    assert d[0]["fault_names"] == ["RD_REMOTE", "CW_OFF"]
    assert d[1]["bells_sent"] == 5 and d[1]["fault_names"] == []
    assert d[1]["caps"] == 0
    # Faults cleared on both dies; die 1 left disabled; a narrow path writes halves.
    bits = 32 if narrow else 64
    assert t.writes == [(CTRLS[0], 5, bits), (CTRLS[1], 4, bits)]


class Dram:
    def __init__(self):
        self.bytes = {}

    def write64(self, at, value):
        self.write_block(at, value.to_bytes(8, "little"))

    def read64(self, at):
        return int.from_bytes(self.read_block(at, 8), "little")

    def write_block(self, at, raw):
        for k, b in enumerate(raw):
            self.bytes[at + k] = b

    def read_block(self, at, n):
        return bytes(self.bytes.get(at + k, 0) for k in range(n))


def ring_card():
    heap = iter(range(0x4000_0000, 0x8000_0000, 1 << 24))
    return NS(
        queues={0: NS(base=0x100_0000, size=0), 2: NS(base=0x200_0000, size=0)},
        memory=NS(alloc=lambda n, owner: next(heap)),
        mem=Dram(),
    )


def post(card, ring, n, i, start):
    """The firmware's record i on sysnode n: one vector core (type 2 at (1, 0))."""
    words = [i, start, 100 + i, 1 << 40 | 5, 1, 2 << 32 | 0 << 8 | 1]
    words += [77 << 32 | 55, 3 << 32 | 4] + [0] * 24
    slot = ring.at[n] + i % measure.RING_SLOTS * measure.RING_BYTES
    card.mem.write_block(slot, struct.pack("<32Q", *words))
    card.mem.write64(card.queues[n].base + measure.RING_OFF + 16, i + 1)


def test_the_ring_gives_each_record_once_and_the_last_slots_after_a_wrap(
    monkeypatch,
):
    monkeypatch.setattr(measure, "RING_SLOTS", 4)
    card = ring_card()
    ring = measure.Ring(card)
    assert [q.size for q in card.queues.values()] == [measure.RING_OFF] * 2
    post(card, ring, 0, 0, 1000)
    post(card, ring, 2, 0, 2000)

    first = ring.rows()

    assert [(r["node"], r["start"], r["cycles"]) for r in first] == [
        (0, 1000, 100),
        (2, 2000, 100),
    ]
    assert first[0]["moves"] == 5 and first[0]["units"] == [
        dict(x=1, y=0, type=2, retired=77, busy=55, dbg_hi=3, dbg_lo=4)
    ]
    assert ring.rows() == []
    for i in range(1, 7):
        post(card, ring, 0, i, 1000 + i)
    assert [r["run"] for r in ring.rows()] == [3, 4, 5, 6]


@pytest.mark.parametrize(
    "text, want",
    [
        ("0", {0: 0, 1: 0, 2: 0}),
        ("0x10", {0: 16, 1: 16, 2: 16}),
        ("STEPS+TIMING", {0: 10, 1: 10, 2: 10}),
        ("0:NOENGINE,2:8", {0: 16, 1: 0, 2: 8}),
    ],
)
def test_boot_flags_per_sysnode(text, want):
    assert parse_boot_flags(text, (0, 1, 2)) == want
