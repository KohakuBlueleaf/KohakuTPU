"""The gap `Placement.verify` cannot see: a runner that issues out of plan order.

`verify` proves the PLAN is self-consistent. Lifetimes come from step order, so a
buffer declared early and read late is given an address the allocator reuses, and
the read returns a plausible wrong number. Measured on the card: a per-head
transformer block scored 1.38e-01 instead of 1.07e-03 with `verify()` passing.
"""

import pytest

from ktpu.hw.memplan import FLAT, Plan, PlanError

BIG = FLAT(64, 512)
NSTEP = 6


def chain():
    """A straight chain long enough that the allocator must reuse an address.

    `_pack` allocates a step's writes BEFORE freeing its dead, so no buffer
    aliases the one it reads -- reuse only appears further down the chain.
    """
    p = Plan(arena_words=8 * (BIG.nbytes // 32))
    p.input("t0", BIG)
    for i in range(1, NSTEP):
        p.act(f"t{i}", BIG)
    p.output("out", BIG)
    for i in range(1, NSTEP):
        p.step(f"s{i}", "vmap", reads=(f"t{i - 1}",), writes=(f"t{i}",))
    p.step("last", "vmap", reads=(f"t{NSTEP - 1}",), writes=("out",))
    return p


def aliased_pair(place):
    """An (early, later) pair the allocator put at the same address."""
    al = place.aliases()
    for name in sorted(al, key=lambda n: place.word[n]):
        if al[name]:
            return name, sorted(al[name])[0]
    pytest.fail("nothing aliased; the fixture no longer exercises reuse")


def test_the_allocator_really_does_alias_something():
    """Without this every test below would pass for the wrong reason."""
    place = chain().solve()
    early, later = aliased_pair(place)
    assert place.word[early] == place.word[later]
    assert not place.verify(), "the plan itself is sound; that is the point"


def test_reuse_false_leaves_nothing_aliased():
    place = chain().solve(reuse=False)
    assert place.aliases() == {n: set() for n in place.word}


def test_issuing_in_plan_order_is_accepted():
    tr = chain().solve().tracker()
    tr.wrote("t0")
    for i in range(1, NSTEP):
        tr.check(f"t{i - 1}")
        tr.wrote(f"t{i}")


def test_reading_a_buffer_the_allocator_has_given_away_RAISES():
    place = chain().solve()
    early, later = aliased_pair(place)
    tr = place.tracker()
    tr.wrote(early)
    tr.wrote(later)
    with pytest.raises(PlanError, match="ISSUE ORDER"):
        tr.check(early)


def test_the_message_names_what_overwrote_it():
    place = chain().solve()
    early, later = aliased_pair(place)
    tr = place.tracker()
    tr.wrote(early)
    tr.wrote(later)
    with pytest.raises(PlanError, match=later):
        tr.check(early)


def test_a_never_written_buffer_is_refused_too():
    """On this ECC DRAM an unwritten line is a bus fault, not zeros."""
    place = chain().solve()
    early, _ = aliased_pair(place)
    with pytest.raises(PlanError, match="never written"):
        place.tracker().check(early)
