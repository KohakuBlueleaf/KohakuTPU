"""P4: ring credit and counters. Bursts of 1-8 back-to-back rings from the first
sysnode to each other (more than the interlink's queue of 4 waits on credit), with
the consumer waiting at once or late; every other sysnode bursting into one sink at
once; slot reuse across epochs of changing amounts; the 12-bit dispatch-engine ring
count and the 16-bit doorbell count driven past their wrap, the bells taken in
half-range waits. Every ring is taken, so the interlink's RING fault (a ring
dropped into a full queue) must stay clear; the ring form (package or engine path)
is the runner's boot flag. Each sysnode stores the rings it took.

Then stale tokens: EPOCHS rounds on one slot, the producer settling before each
round's mark store (one word, a new epoch tag each round) and ring, while a third
sysnode (else the producer) rings the consumer's wrong slot EPOCHS times at once.
The consumer copies the mark right after each wait, before its ack: a wrong-slot or
old token that released new work reads POISON or the previous round's epoch.

Every fn carries its token ledger (`h.ledger`): exact packets by source and sent,
and every slot's residue, beside the RING fault.

Not here: the CTRL form (`ctrl il_ring <- V`): the static checker does not count a
CTRL write as a ring, so the consumer's wait reads as a deadlock."""

import itertools

import numpy as np

from h import check
from h.gen import Module, Spec
from h.gen.body import BELL_TAKE, HEAD, Body, dram_at, nearest

BURSTS = range(1, 9)
SINK_BURSTS = (1, 4, 8)
#: The consumer's delay before it waits, in cycles.
DELAYS = (0, 20_000)
EPOCH_AMOUNTS = (1, 2, 3, 5, 8, 13, 21, 34)
#: Past one wrap of the engine's 12-bit ring count, and three half-ranges of bells.
ENGINE_WRAP = 2 * 4096 + 5
BELL_CHUNKS = 3
#: The stale-token rounds, and the producer's settle before each.
EPOCHS, LATE = 4, 20_000
START, DATA, ACK, WRONG = 1, 2, 3, 4
MARK = 1


def took(b: Body, n: int, at: int, value: int) -> None:
    b.add(n, f"r9 = set {value}", f"store count + {8 * at} <- r9")


def burst(p: int, c: int, rings: int, delay: int):
    b = Body(bells=True, ledger=True)
    b.barrier(sorted((p, c)), START)
    for _ in range(rings):
        b.ring(p, c, DATA)
    if delay:
        b.add(c, f"settle {delay}")
    b.take(c, DATA, rings)
    took(b, p, 0, 0)
    took(b, c, 1, rings)
    name = f"p4_burst_{p}{c}_n{rings}_d{delay}"
    return name, b.fn(name, "count: u64[2] @dram"), [0, rings], b


def sink(c: int, sources: list[int], rings: int):
    b = Body(bells=True, ledger=True)
    every = sorted([c, *sources])
    b.barrier(every, START)
    for s in sources:
        for _ in range(rings):
            b.ring(s, c, DATA)
    b.take(c, DATA, rings * len(sources))
    want = [rings * len(sources) if n == c else 0 for n in every]
    for k, n in enumerate(every):
        took(b, n, k, want[k])
    name = f"p4_sink{c}_n{rings}"
    return name, b.fn(name, f"count: u64[{len(every)}] @dram"), want, b


def epochs(p: int, c: int):
    """One slot, a new amount each epoch; the consumer acks each before the next."""
    b = Body(bells=True, ledger=True)
    b.barrier(sorted((p, c)), START)
    for amount in EPOCH_AMOUNTS:
        b.ring(p, c, DATA, amount=amount)
        b.take(p, ACK, 1)
        b.take(c, DATA, amount)
        b.ring(c, p, ACK)
    took(b, p, 0, 0)
    took(b, c, 1, sum(EPOCH_AMOUNTS))
    name = f"p4_epochs_{p}{c}"
    return name, b.fn(name, "count: u64[2] @dram"), [0, sum(EPOCH_AMOUNTS)], b


def engine_wrap(p: int, c: int):
    """ENGINE_WRAP rings in a loop, the consumer late: the producer waits credit
    across the engine count's wrap."""
    b = Body(bells=True, ledger=True)
    b.barrier(sorted((p, c)), START)
    b.add(p, "r1 = set 0", f"r2 = set {ENGINE_WRAP}", "label trip")
    b.ring(p, c, DATA, times=ENGINE_WRAP)
    b.add(p, "r1 = add r1, 1", "goto trip if r1 < r2")
    b.add(c, f"settle {DELAYS[-1]}")
    b.take(c, DATA, ENGINE_WRAP)
    took(b, p, 0, 0)
    took(b, c, 1, ENGINE_WRAP)
    name = f"p4_enginewrap_{p}{c}"
    return name, b.fn(name, "count: u64[2] @dram"), [0, ENGINE_WRAP], b


def bell_wrap(p: int, c: int):
    """BELL_CHUNKS half-ranges of rings, each taken by one `wait bell` and acked
    before the next, so the 16-bit count wraps between waits but never laps."""
    b = Body(bells=True, ledger=True)
    b.barrier(sorted((p, c)), START)
    for k in range(BELL_CHUNKS):
        b.add(p, "r1 = set 0", f"r2 = set {BELL_TAKE}", f"label trip{k}")
        b.ring(p, c, DATA, times=BELL_TAKE)
        b.add(p, "r1 = add r1, 1", f"goto trip{k} if r1 < r2")
        b.take(p, ACK, 1)
        b.bell(c, p, BELL_TAKE)
        b.ring(c, p, ACK)
    total = BELL_CHUNKS * BELL_TAKE
    b.take(c, DATA, total)
    took(b, p, 0, 0)
    took(b, c, 1, total)
    name = f"p4_bellwrap_{p}{c}"
    return name, b.fn(name, "count: u64[2] @dram"), [0, total], b


def stale(p: int, c: int, q: int, board: str):
    """`q` rings the wrong slot EPOCHS times at once (`p` itself on two sysnodes)."""
    b = Body(ledger=True)
    b.barrier(sorted({p, c, q}), START)
    for _ in range(EPOCHS):
        b.ring(q, c, WRONG)
    for e in range(EPOCHS):
        b.add(p, f"settle {LATE}")
        b.stores(p, dram_at("mark", c, board), [0], e, p, MARK)
        b.ring(p, c, DATA)
        b.take(c, DATA, 1)
        b.observe(c, "mark", [0], "seen", e)
        b.ring(c, p, ACK)
        b.take(p, ACK, 1)
    b.take(c, WRONG, EPOCHS)
    name = f"p4_stale_{p}{c}_q{q}"
    return name, b.fn(name, f"mark: u64[1] @dram, seen: u64[{EPOCHS}] @dram"), b


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD, {}
    first, last = sysnodes[0], sysnodes[-1]
    fns = [
        burst(first, c, rings, delay)
        for c, rings, delay in itertools.product(
            nearest(first, sysnodes), BURSTS, DELAYS
        )
    ]
    fns += [
        sink(c, sorted(nearest(c, sysnodes)), rings)
        for c, rings in itertools.product((first, last), SINK_BURSTS)
    ]
    far = nearest(first, sysnodes)[-1]
    fns += [
        f(p, c)
        for f in (epochs, engine_wrap, bell_wrap)
        for p, c in ((first, far), (far, first))
    ]
    for name, src, want, b in fns:
        text += src
        specs[name] = Spec(
            ("count", "slots"),
            lambda _, w=want: {"count": np.array(w, np.uint64)},
            ledger=b.ledger_of(),
        )
    for p, c in itertools.permutations(sysnodes, 2):
        rest = [m for m in nearest(c, sysnodes) if m != p]
        name, src, b = stale(p, c, rest[0] if rest else p, board)
        text += src
        marks = np.array([check.tag(e, p, MARK, 0) for e in range(EPOCHS)], np.uint64)
        specs[name] = Spec(
            ("mark", "seen", "slots"),
            lambda _, m=marks: {"mark": m[-1:], "seen": m},
            ledger=b.ledger_of(),
        )
    return Module(text, specs)
