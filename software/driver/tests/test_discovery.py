"""Mesh enumeration over the agent's raw-flit mailbox against an agent whose
replies take a unit's round trip in wall time: a fast transport (XDMA reads
microseconds apart) must wait for them as a slow one (JTAG) does."""

import time

from kohakuaccel.driver.device import discovery as D
from kohakuaccel.driver.device.flit import _pack, encode_caps, field
from kohakuaccel.driver.device.registers import (
    A_RX_FLIT0,
    A_RX_POP,
    A_RX_STATUS,
    A_TX_FLIT0,
    A_TX_KICK,
    CTRL_READ_RESP,
    FLIT_BITS,
    FLIT_WORDS,
    MAG_BASE,
    POS_BITS,
    T_CU_CTRL,
)
from kohakuaccel.driver.transport.base import MASK64

#: v9t6's die: clusters at (1,1), (1,2), vector cores at (2,1), (2,2).
UNITS = {
    (1, 1): encode_caps(0x4D47),
    (1, 2): encode_caps(0x4D47),
    (2, 1): encode_caps(0x5643),
    (2, 2): encode_caps(0x5643),
}
GRID = D.AgentCaps(flit_width=FLIT_BITS, pos_width=POS_BITS, grid_lo=1, grid_hi=2)


class Agent:
    """The agent's TX/RX windows over units that answer a CU_CTRL read
    `delay` seconds after the kick."""

    def __init__(self, units: dict, delay: float) -> None:
        self.units, self.delay = units, delay
        self.tx = [0] * FLIT_WORDS
        self.rx: list[tuple[float, int]] = []

    def _head(self) -> int | None:
        if self.rx and self.rx[0][0] <= time.perf_counter():
            return self.rx[0][1]
        return None

    def write64(self, addr: int, value: int) -> None:
        off = addr - MAG_BASE
        if A_TX_FLIT0 <= off < A_TX_FLIT0 + 8 * FLIT_WORDS:
            self.tx[(off - A_TX_FLIT0) // 8] = value
        elif off == A_TX_KICK:
            f = sum(w << (64 * i) for i, w in enumerate(self.tx))
            top = FLIT_BITS - 1
            dst = (field(f, top, POS_BITS), field(f, top - POS_BITS, POS_BITS))
            src = (
                field(f, top - 2 * POS_BITS, POS_BITS),
                field(f, top - 3 * POS_BITS, POS_BITS),
            )
            txn, idx = field(f, 267, 8), field(f, 247, 8)
            if dst in self.units:
                reply = _pack(
                    [
                        (src[0], POS_BITS),
                        (src[1], POS_BITS),
                        (dst[0], POS_BITS),
                        (dst[1], POS_BITS),
                        (T_CU_CTRL, 4),
                        (txn, 8),
                        (1, 1),
                        (0, 3),
                        (CTRL_READ_RESP, 8),
                        (idx, 8),
                        (self.units[dst], 64),
                    ]
                )
                self.rx.append((time.perf_counter() + self.delay, reply))
        elif off == A_RX_POP and self._head() is not None:
            self.rx.pop(0)

    def read64(self, addr: int) -> int:
        off = addr - MAG_BASE
        head = self._head()
        if off == A_RX_STATUS:
            return 0 if head is not None else 1 << 16
        if A_RX_FLIT0 <= off < A_RX_FLIT0 + 8 * FLIT_WORDS and head is not None:
            return (head >> (64 * ((off - A_RX_FLIT0) // 8))) & MASK64
        return 0


def found(agent: Agent) -> dict:
    return {
        e.coord: e.name for e in D.enumerate_mesh(agent, GRID, reply=(0, 1))  # type: ignore[arg-type]
    }


def test_replies_slower_than_four_fast_reads_are_waited_for():
    # 200 us: a unit's round trip at a low clock, longer than four XDMA
    # register reads, far shorter than one JTAG read.
    got = found(Agent(UNITS, delay=200e-6))
    assert got == {(1, 1): "MG", (1, 2): "MG", (2, 1): "VC", (2, 2): "VC"}


def test_a_coordinate_nothing_answers_is_not_a_unit():
    assert found(Agent({(1, 1): UNITS[(1, 1)]}, delay=0.0)) == {(1, 1): "MG"}
