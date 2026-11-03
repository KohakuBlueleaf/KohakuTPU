"""The measure image's records (`MEASURE_FIRMWARE`, interp.c `measure`): after each
package, one record a run in a DRAM ring each sysnode's queue names.

    ring = Ring(card)        # before the first run
    card.run(...)
    rows = ring.rows()       # the records written since the last read
"""

import struct

#: Where the image finds its ring (queue + RING_OFF: base, slots, count), and a
#: record: run, start, cycles, MVSTAT, units, then per unit its coordinate,
#: CU_COUNTERS and CU_DBG (KA_MRING_UNITS of them at most).
RING_OFF = 0xFC000
RING_BYTES = 256
RING_SLOTS = 1 << 15
RING_UNITS = 8


def decode(node: int, raw: bytes) -> dict:
    """One record of sysnode `node`."""
    w = struct.unpack("<32Q", raw)
    units = []
    for u in range(min(w[4], RING_UNITS)):
        coord, counters, dbg = w[5 + 3 * u : 8 + 3 * u]
        units.append(
            dict(
                x=coord & 0xFF,
                y=(coord >> 8) & 0xFF,
                type=coord >> 32,
                retired=counters >> 32,
                busy=counters & 0xFFFFFFFF,
                dbg_hi=dbg >> 32,
                dbg_lo=dbg & 0xFFFFFFFF,
            )
        )
    return dict(
        node=node,
        run=w[0],
        start=w[1],
        cycles=w[2],
        moves=w[3] & 0xFFFFFFF,
        units=units,
    )


class Ring:
    """Each of `card`'s sysnodes given a ring in DRAM; its queue ends below it."""

    def __init__(self, card) -> None:
        self.card = card
        #: sysnode -> its ring's address, and the records read so far
        self.at: dict[int, int] = {}
        self.read: dict[int, int] = {}
        #: sysnode -> records overwritten before the last `rows` read them
        self.lost: dict[int, int] = {}
        for n, q in card.queues.items():
            at = card.memory.alloc(RING_SLOTS * RING_BYTES, owner=f"measure:{n}")
            q.size = RING_OFF
            card.mem.write64(q.base + RING_OFF, at)
            card.mem.write64(q.base + RING_OFF + 8, RING_SLOTS)
            card.mem.write64(q.base + RING_OFF + 16, 0)
            self.at[n], self.read[n] = at, 0

    def rows(self) -> list[dict]:
        """The records written since the last call, sysnode by sysnode; of a ring
        that went round since, its last RING_SLOTS (`lost` counts the rest)."""
        card, rows = self.card, []
        for n, at in self.at.items():
            count = card.mem.read64(card.queues[n].base + RING_OFF + 16)
            self.lost[n] = max(0, count - self.read[n] - RING_SLOTS)
            for i in range(max(self.read[n], count - RING_SLOTS), count):
                raw = card.mem.read_block(at + i % RING_SLOTS * RING_BYTES, RING_BYTES)
                rows.append(decode(n, raw))
            self.read[n] = count
        return rows
