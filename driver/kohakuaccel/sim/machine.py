"""An in-process machine: the agent's registers, a memory window, and units.

Enough of a KohakuAccel machine for a driver to be exercised end to end with
nothing attached -- staging, kick, dispatch, execution, completion signalling
and the NODE_STATUS mirror. It models the CONTROL PLANE faithfully and the
datapath not at all: a unit model is ordinary Python and computes however it
likes.

What it does not model: the mesh itself. Flits go straight from the dispatcher
to the addressed unit, so routing, credit backpressure and deadlock are all
invisible here. Those are properties of the RTL and belong to its testbenches.
"""

import abc
from dataclasses import dataclass, field

from kohakuaccel.device.flit import ctrl_reply, ctrl_request
from kohakuaccel.device.registers import (
    A_NODE,
    A_PROG_BASE,
    A_PROG_CRED,
    A_PROG_DST,
    A_PROG_KICK,
    A_PROG_LEN,
    A_PROG_STAT,
    A_RX_FLIT0,
    A_RX_POP,
    A_RX_STATUS,
    A_SIG_DONE,
    A_STAGE,
    A_TX_FLIT0,
    A_TX_KICK,
    A_TX_STATUS,
    CTRL_READ_RESP,
    CU_CAPS,
    FLIT_WORDS,
    MAG_BASE,
    MO_CODE,
    MO_CTRL,
    ORC_BASE,
    SIG_INST_COMPLETE,
)
from kohakuaccel.transport.base import MASK64, WORD_BYTES, Transport, aligned

#: Where this simulated machine exposes its memory. A real machine's window
#: comes from its board description; the sim picks one and says so.
MEM_BASE = 0x8000_0000
MEM_SIZE = 1 << 26


@dataclass
class Signal:
    """One CU_SIGNAL: a centrally allocated code and a unit-defined argument."""

    code: int = SIG_INST_COMPLETE
    arg: int = 0


class Memory:
    """The machine's memory, as a flat mutable buffer."""

    def __init__(self, size: int = MEM_SIZE) -> None:
        self.buf = bytearray(size)

    def read(self, addr: int, nbytes: int) -> bytes:
        return bytes(self.buf[addr : addr + nbytes])

    def write(self, addr: int, data: bytes) -> None:
        self.buf[addr : addr + len(data)] = data


class UnitModel(abc.ABC):
    """What a simulated compute unit must provide.

    `caps` is the CU_CAPS word this unit answers with. :meth:`execute` receives
    one instruction flit and the machine's memory, and returns the signals the
    unit emits -- an empty list is legal and means the instruction retired
    silently, which no dispatcher will ever notice.
    """

    caps: int = 0

    @abc.abstractmethod
    def execute(self, flit: int, mem: Memory) -> list[Signal]: ...

    def cost(self, flit: int) -> int:
        """Cycles the instruction JUST EXECUTED occupied this unit.

        Called after :meth:`execute`, so a flit that runs a loaded image can
        report what the image cost rather than what the flit looks like. The
        framework counts; only the project knows the figures. One is the answer
        for a unit that has not said -- an instruction count, honest and never
        zero.
        """
        return 1

    def control(self, idx: int) -> int:
        """Answer a CU_CTRL read of register `idx`.

        Every endpoint answers CU_CAPS; the rest are optional and read zero,
        which is what an endpoint that ties the port off actually reports.
        """
        return self.caps if idx == CU_CAPS else 0


@dataclass
class _Node:
    """One endpoint's mirror: cumulative count, last code, last argument."""

    count: int = 0
    code: int = 0
    arg: int = 0
    seen: bool = False

    def word(self) -> int:
        if not self.seen:
            return 0
        return (
            ((self.code & 0xFF) << 56)
            | ((self.arg & 0xFFFF_FFFF) << 24)
            | ((self.count & 0xFFFF) << 8)
            | 1
        )


@dataclass
class SimMachine(Transport):
    """A machine that answers on the same registers a real one does.

    `units` maps ``(x, y)`` to a :class:`UnitModel`. A kick addressed at a
    coordinate with no unit raises, because on real silicon that is a program
    aimed at nothing and it is worth failing loudly here rather than hanging on
    a completion that will not arrive.
    """

    units: dict[tuple[int, int], UnitModel] = field(default_factory=dict)
    mem: Memory = field(default_factory=Memory)
    #: Whether to price every flit. OFF is the behaviour level -- same numbers,
    #: and no second decode of the flit inside a cluster's `cost`.
    timing: bool = True

    bulk = True

    def __post_init__(self) -> None:
        #: Cycles each unit has been busy, by coordinate. Counted by the
        #: framework, priced by the project's :meth:`UnitModel.cost`.
        self.busy: dict[tuple[int, int], int] = {}
        self.stage: dict[int, int] = {}
        self.nodes: dict[int, _Node] = {}
        self.reg: dict[int, int] = {}
        self.sig_done = 0
        self.done_code = 0
        self.dispatched: list[tuple[int, int, int]] = []
        self.tx: dict[int, int] = {}
        self.rx: list[int] = []

    def write64(self, addr: int, data: int) -> None:
        aligned(addr)
        data &= MASK64
        if MEM_BASE <= addr < MEM_BASE + len(self.mem.buf):
            self.mem.write(addr - MEM_BASE, data.to_bytes(WORD_BYTES, "little"))
            return
        if addr >= MAG_BASE:
            self._mag_write(addr - MAG_BASE, data)
            return
        off = addr - ORC_BASE
        if off == MO_CTRL:
            self.done_code = 0
        self.reg[addr] = data

    def read64(self, addr: int) -> int:
        aligned(addr)
        if MEM_BASE <= addr < MEM_BASE + len(self.mem.buf):
            return int.from_bytes(self.mem.read(addr - MEM_BASE, WORD_BYTES), "little")
        if addr >= MAG_BASE:
            return self._mag_read(addr - MAG_BASE)
        off = addr - ORC_BASE
        if off == MO_CTRL:
            return 0x2  # never busy: dispatch is synchronous here
        if off == MO_CODE:
            return self.done_code
        return self.reg.get(addr, 0)

    def _mag_write(self, off: int, data: int) -> None:
        if off >= A_STAGE:
            self.stage[(off - A_STAGE) // WORD_BYTES] = data
            return
        if A_TX_FLIT0 <= off < A_TX_FLIT0 + FLIT_WORDS * WORD_BYTES:
            self.tx[(off - A_TX_FLIT0) // WORD_BYTES] = data
            return
        if off == A_TX_KICK:
            self._mailbox()
            return
        if off == A_RX_POP:
            if self.rx:
                self.rx.pop(0)
            return
        if off == A_SIG_DONE:
            self.sig_done = 0
            return
        self.reg[MAG_BASE + off] = data
        if off == A_PROG_KICK and data:
            self._kick()

    def _mag_read(self, off: int) -> int:
        if A_NODE <= off < A_STAGE:
            idx = (off - A_NODE) // WORD_BYTES
            node = self.nodes.get(idx)
            return node.word() if node else 0
        if A_RX_FLIT0 <= off < A_RX_FLIT0 + FLIT_WORDS * WORD_BYTES:
            w = (off - A_RX_FLIT0) // WORD_BYTES
            return (self.rx[0] >> (w * 64)) & MASK64 if self.rx else 0
        if off == A_RX_STATUS:
            return 0 if self.rx else (1 << 16)  # [16] empty
        if off == A_TX_STATUS:
            return 0  # never full: the mailbox answers synchronously
        if off == A_SIG_DONE:
            return self.sig_done
        if off == A_PROG_STAT:
            return 0  # run == 0; the dispatcher is never mid-stream here
        return self.reg.get(MAG_BASE + off, 0)

    def _mailbox(self) -> None:
        """Answer a staged CU_CTRL read from the addressed unit.

        The mailbox is the only path to a node that is not a dispatch, and so
        the only way to ask a unit what it is rather than assuming. A read of a
        coordinate with no unit is left unanswered, which is what the caller's
        empty-RX check is there to notice.
        """
        flit = 0
        for w in range(FLIT_WORDS):
            flit |= self.tx.get(w, 0) << (w * 64)
        req = ctrl_reply(flit)
        dst = (
            (flit >> (288 - 4)) & 0xF,
            (flit >> (288 - 8)) & 0xF,
        )
        unit = self.units.get(dst)
        if unit is None:
            return
        value = unit.control(req["idx"])
        reply = ctrl_request(dst=(0, 0), src=dst, idx=req["idx"], txn=req["txn"])
        reply |= (CTRL_READ_RESP & 0xFF) << 248
        reply |= (value & MASK64) << 176
        self.rx.append(reply)

    def _kick(self) -> None:
        """Stream the staged program at PROG_DST and record what it signalled."""
        idx = self.reg.get(MAG_BASE + A_PROG_DST, 0)
        base = self.reg.get(MAG_BASE + A_PROG_BASE, 0)
        nflits = self.reg.get(MAG_BASE + A_PROG_LEN, 0)
        x, y = idx & 0xF, (idx >> 4) & 0xF
        unit = self.units.get((x, y))
        if unit is None:
            raise KeyError(f"kick at ({x},{y}): no unit there, so nothing will reply")

        self.dispatched.append((x, y, nflits))
        # Each unit runs its own stream, so a kick advances THAT unit's clock.
        # The machine's elapsed time is the busiest one, never the sum.
        for slot in range(base, base + nflits):
            flit = self._flit(slot)
            signals = unit.execute(flit, self.mem)
            if self.timing:
                # AFTER executing: a flit that runs a loaded image costs what
                # the image did, which is not a function of the flit.
                self.busy[x, y] = self.busy.get((x, y), 0) + int(unit.cost(flit) or 0)
            for sig in signals:
                self._signal(idx, sig)

    def _flit(self, slot: int) -> int:
        """Reassemble one 288-bit flit from its five staged words."""
        flit = 0
        for w in range(FLIT_WORDS):
            flit |= self.stage.get(slot * FLIT_WORDS + w, 0) << (w * 64)
        return flit

    def _signal(self, idx: int, sig: Signal) -> None:
        node = self.nodes.setdefault(idx, _Node())
        node.count = (node.count + 1) & 0xFFFF
        node.code, node.arg, node.seen = sig.code, sig.arg, True
        self.sig_done = (self.sig_done + 1) & 0xFFFF

    @property
    def elapsed(self) -> int:
        """Cycles the busiest unit spent. Every unit runs its own stream.

        A LOWER bound: nothing here models a unit WAITING, so a dependent chain
        reads as its longest single unit rather than as the chain. Compare
        against `analysis.timing`, whose bracket is the other bound -- if the
        two disagree, one of them has the schedule wrong.

        Zero when `timing` is off, which is the behaviour level rather than a
        run that took no time.
        """
        return max(self.busy.values(), default=0)

    def upload(self, addr: int, data: bytes) -> None:
        """Place `data` at a machine address, as the host would before GO."""
        self.mem.write(addr, data)

    def download(self, addr: int, nbytes: int) -> bytes:
        """Read `nbytes` back from a machine address."""
        return self.mem.read(addr, nbytes)

    def credit(self) -> int:
        """Whatever was last seeded into PROG_CRED."""
        return self.reg.get(MAG_BASE + A_PROG_CRED, 0)
