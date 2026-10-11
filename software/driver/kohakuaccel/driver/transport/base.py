"""A 64-bit AXI window. Two methods are the entire hardware dependency.

Everything above this file is ordinary software: a driver written against
``write64`` / ``read64`` runs unchanged against an in-process model, a JTAG-AXI
master, or PCIe XDMA. Only the backend differs.

This module is FRAMEWORK in the strictest sense -- it names no accelerator, no
compute unit and no number format, and it is the one part of the stack that
would be identical for any machine built on KohakuAccel.
"""

import abc

WORD_BYTES = 8  # the transport's word; every address is a BYTE address
MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1


class TransportUnavailable(RuntimeError):
    """This backend cannot be reached, and the message says why.

    Distinct from an I/O failure ON a backend that opened: absence is a
    configuration answer -- wrong host, driver not installed, card not
    enumerated -- and a caller that can fall back to another transport needs to
    tell the two apart without reading a platform error code.
    """


class Transport(abc.ABC):
    """A 64-bit AXI window.

    THE BLOCK METHODS ARE NOT A SECOND CONTRACT. A block at `addr` must be
    indistinguishable from ``len(data) // 8`` word accesses at ascending
    addresses, little-endian -- which is exactly what they do by default, so a
    backend overrides them only when it has a transfer that beats the loop.
    That equivalence is what lets one test compare a bulk backend against
    recorded word writes and expect the same bytes at the same addresses.

    `bulk` says whether overriding happened, because the CALLER has work to do
    either way: coalescing scattered writes into runs is worth the arithmetic
    over PCIe, where a block is one descriptor and the loop is one per word,
    and worth nothing over a transport that will only unpack it again.
    """

    bulk = False

    @abc.abstractmethod
    def write64(self, addr: int, data: int) -> None: ...

    @abc.abstractmethod
    def read64(self, addr: int) -> int: ...

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        for off in range(0, len(data), WORD_BYTES):
            word = int.from_bytes(data[off : off + WORD_BYTES], "little")
            self.write64(addr + off, word)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        out = bytearray()
        for off in range(0, nbytes, WORD_BYTES):
            out += (self.read64(addr + off) & MASK64).to_bytes(WORD_BYTES, "little")
        return bytes(out)


def require_words(nbytes: int) -> None:
    """Raise unless `nbytes` is a whole number of 64-bit words.

    A partial word has no sequence of word accesses to be equivalent to, so
    there is nothing for a backend to fall back to and nothing to compare
    against. Refused at the boundary rather than truncated.
    """
    if nbytes % WORD_BYTES:
        raise ValueError(f"{nbytes} bytes is not a whole number of 64-bit words")


def aligned(addr: int) -> None:
    """A word access to an unaligned address is a different access."""
    if addr % WORD_BYTES:
        raise ValueError(f"address {addr:#x} is not 64-bit aligned")


def runs(pairs):
    """Group word writes into contiguous ``(addr, bytes)`` blocks.

    A run breaks wherever the next address is not the last plus eight, so
    replaying the blocks writes the same bytes to the same addresses in the same
    order -- the grouping is an encoding of the list, not a reordering of it.
    Staging walks slots in address order, so a round's whole staging window
    collapses to one block: one DMA descriptor rather than hundreds.
    """
    out = []
    for addr, data in pairs:
        word = (data & MASK64).to_bytes(WORD_BYTES, "little")
        if out and addr == out[-1][0] + len(out[-1][1]):
            out[-1][1].extend(word)
        else:
            out.append((addr, bytearray(word)))
    return [(a, bytes(b)) for a, b in out]
