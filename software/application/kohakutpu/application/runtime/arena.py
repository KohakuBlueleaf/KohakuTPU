"""A first-fit allocator over one address range, spans coalesced on free."""

import bisect
import os


class OutOfMemory(MemoryError):
    """No free span is large enough."""


class Arena:
    """Spans of [lo, hi): `align`-aligned starts, sizes rounded to `align`."""

    def __init__(self, lo: int, hi: int, align: int = 256) -> None:
        if hi <= lo:
            raise ValueError(f"an empty arena [{lo:#x}, {hi:#x})")
        self.lo, self.hi, self.align = lo, hi, align
        start = -(-lo // align) * align
        self.free_list: list = [(start, hi - start)]  # sorted (addr, size)
        self.live: dict = {}  # addr -> reserved size
        self.owners: dict = {}
        self.generations: dict = {}
        self.closed = False
        self.requested: dict = {}
        self.references: dict = {}
        self.released: set = set()
        self.peak = 0
        self.allocations = 0
        self.fast_lookup = os.environ.get("KOHAKUTPU_ARENA_LOOKUP", "1") == "1"

    def _track(self, at: int, size: int, nbytes: int, owner: str) -> int:
        self.live[at] = size
        self.requested[at] = int(nbytes)
        self.owners[at] = owner
        self.references[at] = 0
        self.allocations += 1
        self.generations[at] = self.allocations
        self.peak = max(self.peak, self.used)
        return at

    def alloc(self, nbytes: int, owner: str = "runtime") -> int:
        if self.closed:
            raise RuntimeError("arena is closed")
        size = max(self.align, -(-int(nbytes) // self.align) * self.align)
        for i, (at, n) in enumerate(self.free_list):
            if n >= size:
                if n == size:
                    self.free_list.pop(i)
                else:
                    self.free_list[i] = (at + size, n - size)
                return self._track(at, size, nbytes, owner)
        raise OutOfMemory(
            f"{nbytes:,} bytes do not fit: {self.free:,} free in "
            f"{len(self.free_list)} spans, the largest {self.largest:,}"
        )

    def alloc_top(self, nbytes: int, owner: str = "reservation") -> int:
        """A span at the top of the highest free span that holds it: kept
        clear of the first-fit allocations growing from below."""
        size = max(self.align, -(-int(nbytes) // self.align) * self.align)
        for i in range(len(self.free_list) - 1, -1, -1):
            at, n = self.free_list[i]
            top = (at + n - size) // self.align * self.align
            if top < at:
                continue
            rest = [(at, top - at)] if top > at else []
            if at + n > top + size:
                rest.append((top + size, at + n - top - size))
            self.free_list[i : i + 1] = rest
            return self._track(top, size, nbytes, owner)
        raise OutOfMemory(
            f"{nbytes:,} bytes do not fit: {self.free:,} free in "
            f"{len(self.free_list)} spans, the largest {self.largest:,}"
        )

    def containing(self, addr: int) -> int:
        """The allocation containing a byte address, including a view offset."""
        if self.fast_lookup and addr in self.live:
            return addr
        for at, size in self.live.items():
            if at <= addr < at + size:
                return at
        raise KeyError(f"{addr:#x} was never allocated")

    def retain(self, addr: int) -> int:
        at = self.containing(addr)
        if at in self.released:
            raise ValueError(f"{at:#x} has no live owner")
        self.references[at] += 1
        return at

    def retain_pinned(self, at: int) -> None:
        """Extend an existing reference, even after its owner has freed it."""
        if self.references[at] <= 0:
            raise ValueError(f"{at:#x} has no pin to retain")
        self.references[at] += 1

    def reuse_pinned(self, at: int) -> None:
        """A new owner of a released, pinned span; older handles stay revoked."""
        if self.closed or at not in self.released or self.references.get(at) != 1:
            raise ValueError(f"{at:#x} is not a released, pinned allocation")
        self.released.remove(at)
        self.allocations += 1
        self.generations[at] = self.allocations

    def close(self) -> None:
        """Revoke all spans after the device has stopped accessing memory."""
        self.closed = True
        for at in list(self.live):
            self.released.add(at)
            self._release(at)

    def unretain(self, at: int) -> None:
        if self.closed:
            return
        if self.references[at] <= 0:
            raise ValueError(f"{at:#x} has no reference to release")
        self.references[at] -= 1
        if not self.references[at] and at in self.released:
            self._release(at)

    def release(self, addr: int) -> None:
        if addr not in self.live or addr in self.released:
            raise KeyError(f"{addr:#x} has no live owner")
        self.released.add(addr)
        if not self.references[addr]:
            self._release(addr)

    def _release(self, addr: int) -> None:
        size = self.live.pop(addr)
        self.owners.pop(addr)
        self.requested.pop(addr)
        self.references.pop(addr)
        self.generations.pop(addr, None)
        self.released.remove(addr)
        i = bisect.bisect_left(self.free_list, (addr, 0))
        self.free_list.insert(i, (addr, size))
        # Coalesce with the neighbours.
        if i + 1 < len(self.free_list):
            a, n = self.free_list[i]
            b, m = self.free_list[i + 1]
            if a + n == b:
                self.free_list[i : i + 2] = [(a, n + m)]
        if i > 0:
            a, n = self.free_list[i - 1]
            b, m = self.free_list[i]
            if a + n == b:
                self.free_list[i - 1 : i + 1] = [(a, n + m)]

    def stats(self) -> dict:
        """Reservations, logical usage and external fragmentation in bytes."""
        owners: dict = {}
        for at, size in self.live.items():
            owner = self.owners[at]
            owners[owner] = owners.get(owner, 0) + size
        return {
            "used": self.used,
            "requested": sum(self.requested.values()),
            "peak": self.peak,
            "allocations": self.allocations,
            "free": self.free,
            "largest": self.largest,
            "free_spans": len(self.free_list),
            "fragmented": self.free - self.largest,
            "owners": owners,
        }

    @property
    def free(self) -> int:
        return sum(n for _, n in self.free_list)

    @property
    def used(self) -> int:
        return sum(self.live.values())

    @property
    def largest(self) -> int:
        return max((n for _, n in self.free_list), default=0)


__all__ = ["Arena", "OutOfMemory"]
