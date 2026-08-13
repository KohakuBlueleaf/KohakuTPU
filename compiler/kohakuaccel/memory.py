"""Device memory: allocation, and the contract a byte order has to satisfy.

Level 2. Addresses and byte order are decided here. A layout is opaque to the
framework beyond four things: it can size itself, pack an array, unpack one
back, and two layouts with the same `key` hold the same bytes.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


class OutOfMemory(RuntimeError):
    """The arena is full, and the message says by how much."""


@runtime_checkable
class Layout(Protocol):
    """One byte order for an array of a given shape."""

    @property
    def key(self) -> str:
        """Identity for caching. Equal keys mean interchangeable bytes."""

    def nbytes(self, shape: tuple) -> int:
        """Bytes an array of `shape` occupies in this order."""

    def pack(self, array) -> bytes:
        """`array` as the bytes the device expects."""

    def unpack(self, raw: bytes, shape: tuple):
        """Bytes from the device back into a row-major array of `shape`."""


@dataclass
class Arena:
    """A free-list allocator over a span of device memory.

    Every allocation is aligned to `align`, freed spans coalesce with their
    neighbours, and a span is freed when nothing references it.
    """

    base: int
    size: int
    align: int = 256
    #: Which mesh's memory this spans, when the caller knows. An address carries
    #: its mesh in bits [33:32], so on a group of four "full" is not one answer.
    mesh: int | None = None
    top: int = field(init=False)
    free_list: list = field(default_factory=list, init=False)
    live: dict = field(default_factory=dict, init=False)
    peak: int = field(default=0, init=False)
    #: Bumped by :meth:`reset`. An address handed out under an older epoch names
    #: somebody else's bytes now, so a holder can tell its span went stale.
    epoch: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.top = self.base

    def alloc(self, nbytes: int) -> int:
        """Reserve `nbytes`, reusing the first freed span that fits.

        Raises :class:`OutOfMemory` when nothing fits.
        """
        # Never zero: a zero-length span would sit on the next allocation's
        # address, and freeing either would hand out bytes the other still owns.
        want = max(self.align, -(-nbytes // self.align) * self.align)
        # BEST fit, not first: a remainder too small for anything IS the
        # fragmentation. Ties to the lowest address, so the top stays trimmable.
        pick = min(
            (i for i, (_, have) in enumerate(self.free_list) if have >= want),
            key=lambda i: (self.free_list[i][1], self.free_list[i][0]),
            default=None,
        )
        if pick is not None:
            start, have = self.free_list.pop(pick)
            if have > want:
                self._give(start + want, have - want)
            self.live[start] = want
            return start
        start = self.top
        if start + want > self.base + self.size:
            where = "" if self.mesh is None else f"mesh_{self.mesh}: "
            raise OutOfMemory(
                f"{where}{nbytes} bytes does not fit: {self.used:,} of "
                f"{self.size:,} used, largest free span {self.largest_free:,}"
            )
        self.top = start + want
        self.peak = max(self.peak, self.top - self.base)
        self.live[start] = want
        return start

    def release(self, addr: int) -> None:
        """Return a span. Raises :class:`KeyError` if it was never allocated."""
        self._give(addr, self.live.pop(addr))

    def _give(self, start: int, size: int) -> None:
        spans = sorted([*self.free_list, (start, size)])
        merged: list = []
        for w, n in spans:
            if merged and merged[-1][0] + merged[-1][1] == w:
                merged[-1] = (merged[-1][0], merged[-1][1] + n)
            else:
                merged.append((w, n))
        self.free_list = merged

    def trim(self) -> int:
        """Give back free spans at the top. Returns the bytes returned.

        Reclaims nothing that is live, so an address already handed out still
        means what it meant. `free_list` is address-sorted, so the spans at the
        top are the tail of it.
        """
        freed = 0
        while self.free_list and sum(self.free_list[-1]) == self.top:
            start, span = self.free_list.pop()
            self.top = start
            freed += span
        return freed

    def reset(self) -> None:
        """Reclaim everything, live or not, and start a new epoch.

        Every address handed out before this names somebody else's bytes after
        it; :attr:`epoch` is what lets a holder tell.
        """
        self.top = self.base
        self.free_list.clear()
        self.live.clear()
        self.epoch += 1

    @property
    def used(self) -> int:
        return sum(self.live.values())

    @property
    def free(self) -> int:
        """Bytes still available, fragmentation ignored.

        The ceiling on what could be handed out, not a promise: one allocation
        of this many bytes fits only if :attr:`largest_free` is as big.
        """
        return self.size - self.used

    @property
    def largest_free(self) -> int:
        spare = self.base + self.size - self.top
        return max([n for _, n in self.free_list] + [spare])

    @property
    def fragmentation(self) -> float:
        """Free bytes NOT in the largest span, over free bytes. 0.0 when whole.

        What an allocation of `free` bytes would fail on. Rises as the free list
        breaks into pieces nothing fits in, which is the failure a bigger arena
        does not fix.
        """
        spare = self.free
        return 0.0 if spare <= 0 else 1.0 - self.largest_free / spare

    def stats(self) -> dict:
        return {
            "live": len(self.live),
            "used": self.used,
            "peak": self.peak,
            "free_spans": len(self.free_list),
            "largest_free": self.largest_free,
            "fragmentation": self.fragmentation,
        }


@dataclass(frozen=True)
class Batched:
    """`count` independent blocks of `inner`, one per batch element.

    `pad` holds the stride still when a buffer is reordered mid-kernel: the
    orders have different sizes and an instance computed its base from the
    stride, so every order of one buffer is given the widest one's.
    """

    inner: Layout
    count: int
    block: tuple
    pad: int = 0

    @property
    def key(self) -> str:
        return f"batch{self.count}:{self.inner.key}"

    @property
    def stride(self) -> int:
        """Bytes between one batch element and the next."""
        return max(self.inner.nbytes(self.block), self.pad)

    def nbytes(self, shape: tuple) -> int:
        return self.count * self.stride

    def pack(self, array) -> bytes:
        flat = np.asarray(array).reshape((self.count, *self.block))
        step = self.stride
        out = bytearray(self.count * step)
        for i in range(self.count):
            blob = self.inner.pack(flat[i])
            out[i * step : i * step + len(blob)] = blob
        return bytes(out)

    def unpack(self, raw: bytes, shape: tuple):
        step = self.stride
        span = self.inner.nbytes(self.block)
        blocks = [
            self.inner.unpack(raw[i * step : i * step + span], self.block)
            for i in range(self.count)
        ]
        return np.stack(blocks).reshape(shape)


@dataclass(frozen=True)
class Buffer:
    """A span of device memory holding one array in one byte order."""

    addr: int
    shape: tuple
    layout: Layout

    @property
    def nbytes(self) -> int:
        return self.layout.nbytes(self.shape)

    def __repr__(self) -> str:
        return f"Buffer({self.addr:#x}, {self.shape}, {self.layout.key})"
