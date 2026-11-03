"""P2: signal slot amounts. One producer rings each of its 1, 2 or 3 consumers
(nearest first) by amount N a round, as one ring of amount N or as N rings of
amount one (the packet count differs, the amount not); each consumer waits for N
and takes it, then acks; the producer waits all acks before the next round. A
start barrier aligns the sysnodes; each stores its round count.

Then the boundary: N - 1 must not release a wait for N. The producer rings N - 1,
an unrelated source (a third sysnode, else the producer) rings an unrelated slot,
the producer settles, stores a mark and rings the last one; the consumer waits for
N at once (or takes 1, then N - 1) and copies the mark it reads right after its
wait into `seen`. An early release reads POISON.

Every fn carries its token ledger (`h.ledger`). The width boundary is checked at
admission, not timed: the compiler refuses amount 0 (the hardware's 0-counts-1
encoding is unreachable from L1) and amount 65536 (L0.STEP.033)."""

import itertools

from h import check
from h.gen import Module, Spec
from h.gen.body import HEAD, Body, counts, nearest

ROUNDS = (1, 16)
#: Ring amounts: 1/2/4/8, a 12-bit counter's span, the field's largest.
AMOUNTS = (1, 2, 4, 8, 4096, 65535)
#: Amounts also rung as that many amount-one rings.
SPLIT = (2, 4, 8)
#: The boundary's N, and the producer's settle before its last ring.
PARTIAL = (2, 4, 8)
DELAY = 20_000
START, SIGNAL, ACK, OTHER = 1, 2, 3, 4
#: The mark's tag region.
MARK = 1


def fanout(producer: int, consumers: list[int], amount: int, rounds: int, split: bool):
    b = Body(ledger=True)
    every = sorted([producer, *consumers])
    b.barrier(every, START)
    for n in every:
        b.add(n, "r1 = set 0", f"r2 = set {rounds}", "label trip")
    for c in consumers:
        if split:
            for _ in range(amount):
                b.ring(producer, c, SIGNAL, times=rounds)
        else:
            b.ring(producer, c, SIGNAL, amount=amount, times=rounds)
    for c in consumers:
        b.take(c, SIGNAL, amount)
        b.ring(c, producer, ACK, times=rounds)
    b.take(producer, ACK, len(consumers))
    for k, n in enumerate(every):
        b.add(
            n, "r1 = add r1, 1", "goto trip if r1 < r2", f"store count + {8 * k} <- r1"
        )
    tag = "".join(map(str, consumers))
    form = "x1" if split else ""
    name = f"p2_{producer}_to{tag}_a{amount}{form}_k{rounds}"
    return name, b.fn(name, f"count: u64[{len(every)}] @dram"), b


def partial(p: int, c: int, u: int, n: int, whole: bool):
    """`u` the unrelated source (`p` itself on two sysnodes)."""
    b = Body(ledger=True)
    b.barrier(sorted({p, c, u}), START)
    b.ring(p, c, SIGNAL, amount=n - 1)
    b.ring(u, c, OTHER)
    b.add(p, f"settle {DELAY}")
    b.stores(p, "mark", [0], 0, p, MARK)
    b.ring(p, c, SIGNAL)
    if whole:
        b.take(c, SIGNAL, n)
    else:
        b.take(c, SIGNAL, 1)
        b.take(c, SIGNAL, n - 1)
    b.observe(c, "mark", [0], "seen", 0)
    b.take(c, OTHER, 1)
    name = f"p2_partial_{p}{c}_u{u}_n{n}_{'whole' if whole else 'split'}"
    return name, b.fn(name, "mark: u64[1] @dram, seen: u64[1] @dram"), b


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD, {}
    for producer, amount, rounds in itertools.product(sysnodes, AMOUNTS, ROUNDS):
        others = nearest(producer, sysnodes)
        for k, split in itertools.product(range(1, len(others) + 1), (False, True)):
            if split and amount not in SPLIT:
                continue
            name, src, b = fanout(producer, sorted(others[:k]), amount, rounds, split)
            text += src
            specs[name] = Spec(
                ("count", "slots"),
                lambda _, r=rounds, n=k + 1: counts(r, n),
                ledger=b.ledger_of(),
            )
    for (p, c), n, whole in itertools.product(
        itertools.permutations(sysnodes, 2), PARTIAL, (True, False)
    ):
        rest = [m for m in nearest(c, sysnodes) if m != p]
        name, src, b = partial(p, c, rest[0] if rest else p, n, whole)
        text += src
        mark = check.tags(0, p, MARK, [0])
        specs[name] = Spec(
            ("mark", "seen", "slots"),
            lambda _, m=mark: {"mark": m, "seen": m},
            ledger=b.ledger_of(),
        )
    return Module(text, specs)
