"""A card's memory with one owner: the host's DRAM arrays and each node's
staging store. Arrays, scratch, packages and firmware regions are spans of
these arenas. Recording pins existing spans instead of reserving a child heap.

    mem = CardMemory((lo, hi), {node: (lo, hi)}, paint=write)
    a = mem.alloc(nbytes)          # DRAM, painted before it is handed out
    s = mem.stage(nbytes, node)    # a node's staging store
    private = mem.carve(nbytes)    # an arena of its own at the top of DRAM
    mem.free(a)
"""

import os

from kohakutpu.application.runtime.arena import Arena
from kohakutpu.compiler import _rs
from kohakutpu.language.dsl.kernel import _MUTANT

#: The most zero bytes one paint write carries.
PAINT_CHUNK = 16 << 20


class CardMemory:
    """DRAM `[lo, hi)` and staging ``{node: (lo, hi)}``. With `paint` (a
    ``write(addr, bytes)`` the host does not log), DRAM is zeroed before an
    allocation first hands it out: a host whose reads are wider than the
    memory's write granule reads whole lines, and never-written ECC DRAM
    answers them with an error."""

    def __init__(self, dram: tuple, staging: dict, paint=None) -> None:
        self.arena_cls = _arena_cls()
        self.dram = self.arena_cls(*dram)
        self.staging = {n: self.arena_cls(lo, hi) for n, (lo, hi) in staging.items()}
        self.paint = paint
        #: DRAM below this is painted. Every DRAM allocation from below paints
        #: up to its end, so no live span lies past the mark.
        self.painted = self.dram.lo
        #: `(stripe bytes, homes)` of the DRAM interleave; node -> its packages' home.
        self.stripes = None
        self.package_homes: dict = {}

    def near(self, node: int) -> tuple | None:
        """Where `node`'s packages go (`arena.place`)."""
        home = self.package_homes.get(node)
        return None if home is None else (*self.stripes, home)

    def alloc(self, nbytes: int, owner: str = "runtime", near=None) -> int:
        """`nbytes` of DRAM, on `near`'s home when they fit there."""
        at = self.dram.alloc(nbytes, owner=owner, near=near)
        end = at + self.dram.live[at]
        if self.paint is not None and end > self.painted:
            _zero(self.paint, self.painted, end - self.painted)
            self.painted = end
        return at

    def stage(self, nbytes: int, node: int, owner: str = "staging") -> int:
        """`nbytes` of `node`'s staging store."""
        return self.staging[node].alloc(nbytes, owner=owner)

    def carve(self, nbytes: int, node: int | None = None) -> Arena:
        """An arena of its own over `nbytes` at the top of DRAM (zeroed now
        when painting), or of `node`'s staging store; held until the card
        closes."""
        arena = self.dram if node is None else self.staging[node]
        at = arena.alloc_top(nbytes)
        size = arena.live[at]
        if node is None and self.paint is not None:
            _zero(self.paint, at, size)
        if _MUTANT.get() == "app.memory/unlatched_carve":
            return _arena_cls()(at, at + size)
        return self.arena_cls(at, at + size)

    def free(self, addr: int) -> None:
        for arena in self.arenas():
            if addr in arena.live:
                arena.release(addr)
                return
        raise KeyError(f"{addr:#x} was never allocated")

    def arenas(self) -> tuple:
        return (self.dram, *self.staging.values())

    def mark(self) -> tuple:
        """What is held now: `release` frees scoped allocations placed since."""
        return tuple(frozenset(a.live) for a in self.arenas())

    def release(self, mark: tuple) -> None:
        """Release scoped spans; queue-owned packages retire on completion/reset."""
        for arena, kept in zip(self.arenas(), mark, strict=True):
            for at in [
                a
                for a in arena.live
                if a not in kept
                and a not in arena.released
                and arena.owners[a] != "package"
            ]:
                arena.release(at)


def package_homes(value: str, board: dict) -> dict:
    """node -> the home its packages start on: `any` none, `near` the home of
    each node's die, else one home a node (``3`` or ``0,2``). Anything else
    raises `ValueError`: a list that names no home for a node, or a home the
    board does not have."""
    if value == "any":
        return {}
    if value == "near":
        homes = board["dram_home_of_die"]
        return {node: homes[die] for node, die in enumerate(board["chain"])}
    homes = [int(home) for home in value.split(",")]
    nodes, count = len(board["chain"]), board["dram_homes"]
    if len(homes) != nodes or not all(0 <= home < count for home in homes):
        raise ValueError(
            f"package home {value!r}: one home of 0..{count - 1} for each of "
            f"{nodes} nodes, or any, or near"
        )
    return dict(enumerate(homes))


def _arena_cls():
    return _rs.Arena if os.environ.get("KTPU_NATIVE_MEMORY", "0") == "1" else Arena


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


__all__ = ["PAINT_CHUNK", "CardMemory", "package_homes"]
