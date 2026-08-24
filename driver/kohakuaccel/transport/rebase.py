"""Mapping the driver's logical address map onto where a card actually puts it.

The driver addresses a fixed logical map -- the agent at :data:`MAG_BASE`, the
control-program engine at :data:`ORC_BASE`. A synthesised mesh does not look like
that: it wires the control window STRAIGHT TO THE AGENT, so the agent's registers
begin at whatever base the block design assigned and there is no command RAM at
all.

This wrapper is where that difference lives, so nothing above it carries a board
constant.
"""

from kohakuaccel.device.registers import MAG_BASE, ORC_BASE
from kohakuaccel.transport.base import WORD_BYTES, Transport


class Rebased(Transport):
    """Put the agent's logical window at `mag_base` on the card.

    An access below :data:`MAG_BASE` is aimed at the control-program engine,
    which a synthesised mesh does not have. That raises rather than silently
    landing somewhere: the host IS the machine that executes the control program
    on a card, so reaching for the engine means a caller took the bench path by
    mistake.
    """

    def __init__(self, inner: Transport, mag_base: int) -> None:
        self.inner = inner
        self.mag_base = mag_base
        self.bulk = inner.bulk

    def _map(self, addr: int) -> int:
        if addr < MAG_BASE:
            raise ValueError(
                f"address {addr:#x} is in the control-program engine's window "
                f"({ORC_BASE:#x}), which a synthesised mesh does not have; run the "
                f"program with Program.execute instead of load/go"
            )
        return self.mag_base + (addr - MAG_BASE)

    def write64(self, addr: int, data: int) -> None:
        self.inner.write64(self._map(addr), data)

    def read64(self, addr: int) -> int:
        return self.inner.read64(self._map(addr))

    def write_block(self, addr: int, data: bytes) -> None:
        self.inner.write_block(self._map(addr), data)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        return self.inner.read_block(self._map(addr), nbytes)

    def __repr__(self) -> str:
        return f"Rebased({self.inner!r}, mag_base={self.mag_base:#x})"


class UnitGlobal(Transport):
    """Unit-global memory addresses, carried over a host whose windows differ.

    A program computes the addresses UNITS issue -- ``(mesh << shift) | local``
    -- but a board may expose each mesh's DRAM to the HOST at another base
    entirely (v6.5: units say ``N << 36``, the host window is ``(N+1) << 40``).
    This translates per access, from the board's own numbers, so the runtime
    never learns the difference exists.
    """

    def __init__(self, inner: Transport, dram_bases, mem_bases, size) -> None:
        self.inner = inner
        self.bulk = inner.bulk
        self.size = size
        self._spans = sorted(zip(dram_bases, mem_bases), reverse=True)

    def _map(self, addr: int, nbytes: int = WORD_BYTES) -> int:
        for dram, mem in self._spans:
            if addr >= dram:
                local = addr - dram
                if local + nbytes > self.size:
                    break
                return mem + local
        raise ValueError(
            f"{addr:#x} is in no mesh's DRAM: bases are "
            f"{[hex(d) for d, _ in self._spans]}, each {self.size:#x} bytes"
        )

    def write64(self, addr: int, data: int) -> None:
        self.inner.write64(self._map(addr), data)

    def read64(self, addr: int) -> int:
        return self.inner.read64(self._map(addr))

    def write_block(self, addr: int, data: bytes) -> None:
        self.inner.write_block(self._map(addr, len(data)), data)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        return self.inner.read_block(self._map(addr, nbytes), nbytes)

    def __repr__(self) -> str:
        return f"UnitGlobal({self.inner!r}, {len(self._spans)} meshes)"


class Window(Transport):
    """`size` bytes of the card starting at `base`, addressed from zero.

    For memory rather than registers. A machine with several meshes gives each
    one its own DRAM at its own base, and an address a program computed is an
    offset into ITS memory. Raises :class:`ValueError` on an access that runs off
    the end, which would otherwise land silently in the next mesh's memory.

    `granule` is the endpoint's write width. THIS ENDPOINT DOES NOT HONOUR BYTE
    STROBES: a write shorter than one granule paints the whole granule and
    ZERO-FILLS every byte the beat did not carry, so a partial write destroys its
    neighbours and reports success. Measured on v7 2026-08-23 -- four consecutive
    `write64` into one 32-byte line leave only the last, and one `write64` into a
    fully-written line zeroes the other three words. Non-zero refuses those
    writes rather than letting them corrupt.
    """

    def __init__(
        self,
        inner: Transport,
        base: int,
        size: int | None = None,
        granule: int = 0,
    ) -> None:
        self.inner = inner
        self.base, self.size = base, size
        self.granule = granule
        self.bulk = inner.bulk

    def _check_granule(self, addr: int, nbytes: int) -> None:
        """Refuse a write this endpoint cannot perform without collateral."""
        g = self.granule
        if not g or (addr % g == 0 and nbytes % g == 0):
            return
        raise ValueError(
            f"[{addr:#x}, {addr + nbytes:#x}) is not a whole {g}-byte write. This "
            f"endpoint ignores byte strobes, so the rest of each touched granule "
            f"would be ZEROED and the write would still report success. "
            f"Read-modify-write the whole granule, or use write_block on an "
            f"aligned multiple of {g}."
        )

    def _map(self, addr: int, nbytes: int = WORD_BYTES) -> int:
        if addr < 0 or (self.size is not None and addr + nbytes > self.size):
            raise ValueError(
                f"[{addr:#x}, {addr + nbytes:#x}) is outside this window's "
                f"{self.size:#x} bytes at {self.base:#x}"
            )
        return self.base + addr

    def write64(self, addr: int, data: int) -> None:
        self._check_granule(addr, WORD_BYTES)
        self.inner.write64(self._map(addr), data)

    def read64(self, addr: int) -> int:
        return self.inner.read64(self._map(addr))

    def write_block(self, addr: int, data: bytes) -> None:
        self._check_granule(addr, len(data))
        self.inner.write_block(self._map(addr, len(data)), data)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        return self.inner.read_block(self._map(addr, nbytes), nbytes)

    def __repr__(self) -> str:
        size = "unbounded" if self.size is None else f"{self.size:#x}"
        gran = "" if not self.granule else f", granule={self.granule}"
        return f"Window({self.inner!r}, base={self.base:#x}, size={size}{gran})"
