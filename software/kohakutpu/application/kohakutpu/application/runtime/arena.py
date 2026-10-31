"""A first-fit allocator over one address range, spans coalesced on free."""

import bisect
import os

from kohakutpu.compiler import _rs
from kohakutpu.language.dsl.kernel import _MUTANT

OutOfMemory = _rs.OutOfMemory


def home_start(at: int, n: int, size: int, near: tuple) -> int | None:
    """Where span `(at, n)` holds `size` bytes starting on a home, `near`
    being ``(stripe bytes, homes, home)``: the bytes that fit one stripe stay
    on it, so a size past the stripe starts at one. None: nowhere."""
    stripe, homes, home = near
    period = stripe * homes
    base = at // period * period + home * stripe
    stays = 1 if _MUTANT.get() == "app.arena/home_start_only" else min(size, stripe)
    if at + stays > base + stripe:
        base += period
    start = max(at, base)
    return start if start + size <= at + n else None


def place(spans: list, size: int, near: tuple | None = None) -> int | None:
    """Take `size` bytes from the sorted free `spans` and return their address:
    the first span that holds them on `near`'s home, or with no such span (or
    no `near`) the first that holds them at all. None: no span does."""
    found = None
    if near is not None:
        for i, (at, n) in enumerate(spans):
            start = home_start(at, n, size, near)
            if start is not None:
                found = i, start
                break
    if found is None:
        i = next((i for i, (_, n) in enumerate(spans) if n >= size), None)
        if i is None:
            return None
        found = i, spans[i][0]
    i, start = found
    at, n = spans[i]
    rest = [(at, start - at)] if start > at else []
    if at + n > start + size:
        rest.append((start + size, at + n - start - size))
    spans[i : i + 1] = rest
    return start


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
        self.high_water = start
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
        self.high_water = max(self.high_water, at + size)
        return at

    def alloc(self, nbytes: int, owner: str = "runtime", near=None) -> int:
        """`nbytes`, on `near`'s home when a span holds them there (`place`)."""
        if self.closed:
            raise RuntimeError("arena is closed")
        size = max(self.align, -(-int(nbytes) // self.align) * self.align)
        at = place(self.free_list, size, near)
        if at is not None:
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

    def free_spans(self) -> list[int]:
        return [n for _, n in self.free_list]

    def fits(self, sizes) -> bool:
        spans = self.free_spans()
        for size in sizes:
            index = next((i for i, free in enumerate(spans) if free >= size), None)
            if index is None:
                return False
            spans[index] -= size
        return True

    @property
    def free(self) -> int:
        return sum(n for _, n in self.free_list)

    @property
    def used(self) -> int:
        return sum(self.live.values())

    @property
    def largest(self) -> int:
        return max((n for _, n in self.free_list), default=0)


__all__ = ["Arena", "OutOfMemory", "home_start", "place"]
