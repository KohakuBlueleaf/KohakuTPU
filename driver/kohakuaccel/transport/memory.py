"""Transports that need no hardware: one that records, one that remembers.

Between them these make most of a driver testable with nothing attached, which
is what lets the example in ``driver/examples/`` run in-process.
"""

from kohakuaccel.transport.base import (
    MASK64,
    WORD_BYTES,
    Transport,
    aligned,
    require_words,
)


class RecordingTransport(Transport):
    """Records writes instead of performing them.

    Building a control program is a pure function, so it can be checked by
    comparing the recorded transactions against a known-good list.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def write64(self, addr: int, data: int) -> None:
        self.writes.append((addr, data))

    def read64(self, addr: int) -> int:
        raise NotImplementedError("RecordingTransport cannot read")


class MemoryTransport(Transport):
    """A window backed by a dict, so a driver can run with nothing attached.

    RecordingTransport cannot read, and half of any driver -- every poll, every
    result readback -- is reads. This holds what was written and hands it back.

    `on_read` maps an address to a callable taking the attempt count and
    returning the value. A register that only matches on the nth read is the one
    thing a static dict cannot express, and it is exactly what a poll loop has
    to be tested against.
    """

    bulk = True

    def __init__(self, on_read=None) -> None:
        self.mem: dict[int, int] = {}
        self.on_read = dict(on_read or {})
        self.reads: dict[int, int] = {}
        self.blocks: list[tuple[int, int]] = []  # (addr, nbytes) of bulk accesses

    def write64(self, addr: int, data: int) -> None:
        aligned(addr)
        self.mem[addr] = data & MASK64

    def read64(self, addr: int) -> int:
        aligned(addr)
        n = self.reads[addr] = self.reads.get(addr, 0) + 1
        if addr in self.on_read:
            return self.on_read[addr](n) & MASK64
        return self.mem.get(addr, 0)

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        self.blocks.append((addr, len(data)))
        for off in range(0, len(data), WORD_BYTES):
            self.write64(addr + off, int.from_bytes(data[off : off + 8], "little"))

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        self.blocks.append((addr, nbytes))
        out = bytearray()
        for off in range(0, nbytes, WORD_BYTES):
            out += self.read64(addr + off).to_bytes(WORD_BYTES, "little")
        return bytes(out)
