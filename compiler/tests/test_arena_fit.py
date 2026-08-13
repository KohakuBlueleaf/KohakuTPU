"""What the allocator does under churn, and what it refuses.

Compiler-only: an `Arena` is bytes and addresses, so nothing here needs a kernel
or a device. The measured claim is that BEST fit leaves a usable free list where
first fit does not -- an allocator is judged by the allocation it can still
serve, not by the bytes it has left.
"""

import pytest
from kohakuaccel.memory import Arena, OutOfMemory

K = 1024


def churn(arena, rounds=64):
    """Allocate three sizes and free the middle one, repeatedly.

    The pattern that fragments: the survivors pin the low addresses and the
    holes between them are the only reuse there is.
    """
    kept = []
    for r in range(rounds):
        small = arena.alloc(1 * K)
        mid = arena.alloc(3 * K)
        big = arena.alloc(7 * K)
        arena.release(mid)
        kept += [small] if r % 2 else [small, big]
        if r % 2:
            arena.release(big)
    return kept


def test_a_freed_span_is_reused_rather_than_grown_past():
    """The top only moves when nothing free fits."""
    a = Arena(base=0, size=1 << 20, align=256)
    first = a.alloc(4 * K)
    a.alloc(4 * K)
    a.release(first)
    assert a.alloc(4 * K) == first


def test_best_fit_takes_the_tightest_span_not_the_first():
    """A 4K request must not eat the 16K span while an 8K one exists.

    First fit does, and the next 16K allocation then has nowhere to go. The
    guards keep the two free spans apart -- released neighbours coalesce, and
    then there is only one span and nothing to choose between.
    """
    a = Arena(base=0, size=1 << 20, align=256)
    big = a.alloc(16 * K)
    a.alloc(K)
    mid = a.alloc(8 * K)
    a.alloc(K)
    a.release(big)
    a.release(mid)
    assert a.stats()["free_spans"] == 2
    assert a.alloc(4 * K) == mid, "a 4K request broke up the 16K span"
    assert a.alloc(16 * K) == big


def test_adjacent_frees_coalesce_into_one_span():
    """Two neighbours freed are one span, or nothing large ever fits again."""
    a = Arena(base=0, size=1 << 20, align=256)
    one, two = a.alloc(8 * K), a.alloc(8 * K)
    a.release(one)
    a.release(two)
    assert a.stats()["free_spans"] == 1
    assert a.alloc(16 * K) == one


def test_churn_leaves_the_free_list_usable():
    """After churn the arena still serves an allocation the size of its peak.

    Fragmentation is reported so a caller can SEE it: bytes free is not the
    question, the largest span is.
    """
    a = Arena(base=0, size=8 << 20, align=256)
    churn(a)
    assert a.fragmentation < 0.5, a.stats()
    assert a.largest_free >= 64 * K


def test_out_of_memory_names_the_largest_span_not_just_the_total():
    """A caller with free bytes and no span needs to be told which it is."""
    a = Arena(base=0, size=64 * K, align=256)
    a.alloc(32 * K)
    with pytest.raises(OutOfMemory, match="largest free span"):
        a.alloc(48 * K)


def test_fragmentation_is_zero_on_a_whole_arena():
    """Nothing allocated, nothing freed: one span, and no fragmentation."""
    assert Arena(base=0, size=1 << 20, align=256).fragmentation == 0.0


#: One `CU_DATA` burst: `WBURST` granules of a 256-bit word. A DRAIN moves this
#: much at a time, so a base off it lets one drain reach into the next buffer.
DRAIN_BURST_BYTES = 8 * 32


def test_every_span_starts_on_a_drain_burst():
    """The invariant `memplan.py` states: a drain never straddles two buffers.

    Not a tuning choice -- a DRAIN writes whole bursts whatever the sub-tile
    count says, so a base part way into one corrupts its neighbour and reports
    success. Checked across churn, since reuse hands back interior addresses.
    """
    a = Arena(base=0, size=8 << 20, align=DRAIN_BURST_BYTES)
    held = churn(a)
    for addr in [*held, *a.live]:
        assert addr % DRAIN_BURST_BYTES == 0, f"{addr} splits a drain burst"


def test_a_span_is_never_shorter_than_a_burst():
    """A one-byte buffer still owns a whole burst.

    Rounded UP, so the tail of a drain into it lands on bytes it owns. A span
    shorter than a burst would be written past its end by a legal drain.
    """
    a = Arena(base=0, size=1 << 20, align=DRAIN_BURST_BYTES)
    one, two = a.alloc(1), a.alloc(1)
    assert two - one >= DRAIN_BURST_BYTES


def test_the_card_arena_is_burst_aligned():
    """`rt.py` allocates at `BATCH_BYTES`, which must cover a drain burst.

    Two different quanta -- a vector RUN stores a whole batch, a cluster drains
    a whole burst -- and the allocator has to satisfy the larger.
    """
    from kohakutpu.isa.vecemit import BATCH_BYTES

    assert (
        BATCH_BYTES % DRAIN_BURST_BYTES == 0
    ), f"a batch of {BATCH_BYTES} bytes is not whole drain bursts"
