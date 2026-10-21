"""A card's memory with one owner: the host's DRAM arrays and each node's
staging store past its queue. Every allocation -- arrays, a run's scratch, a
package too large for its queue's heap, a recording's private spans -- is a
span of these arenas, so no two users ever hold one address.

    mem = CardMemory((lo, hi), {node: (lo, hi)}, paint=write)
    a = mem.alloc(nbytes)          # DRAM, painted before it is handed out
    s = mem.stage(nbytes, node)    # a node's staging store
    private = mem.carve(nbytes)    # an arena of its own at the top of DRAM
    mem.free(a)
"""

from kohakutpu.application.runtime.arena import Arena

#: The most zero bytes one paint write carries.
PAINT_CHUNK = 16 << 20


class CardMemory:
    """DRAM `[lo, hi)` and staging ``{node: (lo, hi)}``. With `paint` (a
    ``write(addr, bytes)`` the host does not log), DRAM is zeroed before an
    allocation first hands it out: a host whose reads are wider than the
    memory's write granule reads whole lines, and never-written ECC DRAM
    answers them with an error."""

    def __init__(self, dram: tuple, staging: dict, paint=None) -> None:
        self.dram = Arena(*dram)
        self.staging = {n: Arena(lo, hi) for n, (lo, hi) in staging.items()}
        self.paint = paint
        #: DRAM below this is painted. Every DRAM allocation from below paints
        #: up to its end, so no live span lies past the mark.
        self.painted = self.dram.lo

    def alloc(self, nbytes: int) -> int:
        """`nbytes` of DRAM."""
        at = self.dram.alloc(nbytes)
        end = at + self.dram.live[at]
        if self.paint is not None and end > self.painted:
            _zero(self.paint, self.painted, end - self.painted)
            self.painted = end
        return at

    def stage(self, nbytes: int, node: int) -> int:
        """`nbytes` of `node`'s staging store."""
        return self.staging[node].alloc(nbytes)

    def carve(self, nbytes: int, node: int | None = None) -> Arena:
        """An arena of its own over `nbytes` at the top of DRAM (zeroed now
        when painting), or of `node`'s staging store; held until the card
        closes."""
        arena = self.dram if node is None else self.staging[node]
        at = arena.alloc_top(nbytes)
        size = arena.live[at]
        if node is None and self.paint is not None:
            _zero(self.paint, at, size)
        return Arena(at, at + size)

    def free(self, addr: int) -> None:
        for arena in self.arenas():
            if addr in arena.live:
                arena.release(addr)
                return
        raise KeyError(f"{addr:#x} was never allocated")

    def arenas(self) -> tuple:
        return (self.dram, *self.staging.values())

    def mark(self) -> tuple:
        """What is held now: `release` frees everything allocated since."""
        return tuple(frozenset(a.live) for a in self.arenas())

    def release(self, mark: tuple) -> None:
        for arena, kept in zip(self.arenas(), mark, strict=True):
            for at in [a for a in arena.live if a not in kept]:
                arena.release(at)


def _zero(write, at: int, nbytes: int) -> None:
    """`nbytes` of zeros from `at`, in writes of at most PAINT_CHUNK."""
    zero = b""
    while nbytes:
        n = min(PAINT_CHUNK, nbytes)
        if len(zero) < n:
            zero = bytes(n)
        write(at, zero[:n])
        at += n
        nbytes -= n


__all__ = ["PAINT_CHUNK", "CardMemory"]
