"""The allocator's one invariant: two live spans never overlap.

Everything else about a memory manager is a performance question. Overlap is a
correctness question, and it is silent on the machine -- the second tensor
writes over the first and both reads succeed.

Randomised rather than case-based, and now the only cover: the hand-built cases
for the solved-plan allocator went with `src/ktpu`. This covers the EAGER
allocator, where lifetimes come from Python rather than from step order, by
trying to break the invariant a few thousand times.

Run as ``python -m tests.test_arena`` from ``compiler/``.
"""

import itertools
import random

from kohakuaccel.memory import Arena, OutOfMemory

BASE, SIZE = 0x1000, 1 << 20


def overlaps(live: dict) -> list:
    """Every pair of live spans that intersect. Empty is the invariant."""
    spans = sorted((addr, size) for addr, size in live.items())
    return [(a, b) for (a, sa), (b, _) in itertools.pairwise(spans) if b < a + sa]


def test_a_fresh_arena_hands_out_disjoint_spans() -> None:
    arena = Arena(BASE, SIZE)
    for _ in range(50):
        arena.alloc(random.randint(1, 4096))
    assert not overlaps(arena.live)


def test_random_alloc_and_free_never_overlaps() -> None:
    """The invariant, under a few thousand interleaved allocations and frees."""
    rng = random.Random(0)
    arena = Arena(BASE, SIZE)
    held: list[int] = []
    for _ in range(3000):
        if held and rng.random() < 0.45:
            arena.release(held.pop(rng.randrange(len(held))))
        else:
            try:
                held.append(arena.alloc(rng.randint(1, 8192)))
            except OutOfMemory:
                while held:
                    arena.release(held.pop())
        bad = overlaps(arena.live)
        assert not bad, f"overlapping spans {bad}"


def test_freeing_everything_coalesces_to_one_span() -> None:
    """Fragmentation that never merges turns the arena into dust."""
    arena = Arena(BASE, SIZE)
    addrs = [arena.alloc(1024) for _ in range(16)]
    for a in addrs:
        arena.release(a)
    assert len(arena.free_list) == 1, arena.free_list
    assert arena.used == 0


def test_a_freed_span_is_handed_out_again() -> None:
    arena = Arena(BASE, SIZE)
    first = arena.alloc(4096)
    arena.release(first)
    assert arena.alloc(4096) == first


def test_alignment_survives_reuse() -> None:
    """A DRAIN writes a burst at a time, so every span stays burst-aligned."""
    rng = random.Random(1)
    arena = Arena(BASE, SIZE)
    held = []
    for _ in range(500):
        if held and rng.random() < 0.5:
            arena.release(held.pop())
        else:
            held.append(arena.alloc(rng.randint(1, 3000)))
        for addr in arena.live:
            assert addr % arena.align == 0, f"{addr:#x} is not aligned"


def test_out_of_memory_says_what_was_left() -> None:
    arena = Arena(BASE, 8192)
    arena.alloc(4096)
    try:
        arena.alloc(1 << 20)
    except OutOfMemory as exc:
        assert "largest free span" in str(exc)
        return
    raise AssertionError("a 1 MiB allocation fitted in an 8 KiB arena")


def test_a_zero_byte_request_gets_a_span_of_its_own() -> None:
    """Zero rounds up, or it sits on the next allocation's address.

    Two live entries at one address free each other: the first release hands
    out bytes the second still owns, and nothing reports it.
    """
    arena = Arena(BASE, SIZE)
    first, second = arena.alloc(0), arena.alloc(0)
    assert first != second
    assert not overlaps(arena.live)


def test_trim_returns_the_free_tail_and_keeps_the_live_middle() -> None:
    """What `empty_cache` gives back: the top, and only while nothing holds it.

    Trimming a span under a live one would move an address already handed out,
    which is the whole reason this is not simply a reset.
    """
    arena = Arena(BASE, SIZE)
    low, mid, high = arena.alloc(4096), arena.alloc(4096), arena.alloc(4096)
    arena.release(high)
    was = arena.top
    assert arena.trim() == 4096
    assert arena.top < was
    arena.release(low)
    assert arena.trim() == 0, "a free span under a live one is not the tail"
    assert mid in arena.live


def test_reset_starts_a_new_epoch() -> None:
    """A holder cannot otherwise tell that its address was given away.

    `reset` frees LIVE spans, so an address handed out before one names
    somebody else's bytes after it, and freeing it takes memory still in use.
    """
    arena = Arena(BASE, SIZE)
    was = arena.epoch
    arena.alloc(4096)
    arena.reset()
    assert arena.epoch != was
    assert arena.alloc(4096) == BASE


def test_peak_records_the_high_water_mark() -> None:
    arena = Arena(BASE, SIZE)
    a = arena.alloc(4096)
    b = arena.alloc(4096)
    arena.release(a)
    arena.release(b)
    assert arena.peak >= 8192
    assert arena.used == 0


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"  {len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
