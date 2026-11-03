"""P9: CPU stores and fenced publication. A producer's CPU stores K 64-bit tag
words into every other word of y (the words between are lanes no store names: they
must stay POISON), then rings the consumer fenced. The consumer copies all 2K words
of y to `seen` right after its wait, before its ack; the producer waits the ack
before the next of ROUNDS rounds, each a new epoch over the same words. Every
ordered pair: K = 1/2/8/32 into the consumer's staging (peer staging) and
K = 1/32 into canonical DRAM; with K = 8 into staging, the consumer late (it
settles before waiting) and the notify-before-data diagnostic (a plain ring, a
settle, then the stores): its final y is right, its `seen` may not be; a mismatch
is MANIFEST, none INCONCLUSIVE. Then 2 and 3 sources into one sink's staging, each
into its own region of y.

The L1 STORE loop is firmware step plus store; a bundle's cycles over its words is
not an isolated store latency. Package stores go through the uncached alias, so P14
does not gate these rows. Every fn carries its token ledger (faults included).

BLOCKED(sub-word store): L1 `store` writes 64 bits and the firmware refuses an
unaligned address, so partial byte strobes are not expressible; the untouched lanes
here are whole words. BLOCKED(minimal CPU stream): no store stream in the fixed
images other than the package STORE step."""

import itertools

import numpy as np

from h import check
from h.gen import Module, Spec
from h.gen.body import HEAD, Body, nearest, poison

WORDS = (1, 2, 8, 32)
#: Canonical DRAM's word counts; the late and diagnostic cases' (MANY_WORDS).
DRAM_WORDS = (1, 32)
ROUNDS = 4
#: The late consumer's settle, and the diagnostic producer's between ring and data.
LATE = 20_000
SEEN, ACK = 1, 2
#: A source's slot at a many-source sink is SOURCE + its index.
SOURCE = 4
MANY_WORDS = 8


def bundle(b: Body, p: int, k: int, i: int, region: int, base: int) -> None:
    b.stores(p, "y", range(base, base + 2 * k, 2), i, p, region)


def reference(sources: list[int], k: int):
    """y after the last round and `seen` of every round, source by source."""
    y = poison(np.uint64, 2 * k * len(sources))
    seen = []
    for i in range(ROUNDS):
        for q, p in enumerate(sources):
            base = 2 * k * q
            y[base : base + 2 * k : 2] = check.tags(
                i, p, q, range(base, base + 2 * k, 2)
            )
            seen.append(y[base : base + 2 * k].copy())
    out = {"y": y, "seen": np.concatenate(seen)}
    return lambda _: out


def pair(p: int, c: int, k: int, staged: bool, mode: str):
    """`mode`: "now", "late" (the consumer settles first) or "early" (diagnostic)."""
    b = Body(ledger=True)
    for i in range(ROUNDS):
        if mode == "early":
            b.ring(p, c, SEEN, plain=True)
            b.add(p, f"settle {LATE}")
            bundle(b, p, k, i, 0, 0)
        else:
            bundle(b, p, k, i, 0, 0)
            b.ring(p, c, SEEN)
        if mode == "late":
            b.add(c, f"settle {LATE}")
        b.take(c, SEEN, 1)
        b.observe(c, "y", range(2 * k), "seen", 2 * k * i)
        b.ring(c, p, ACK)
        b.take(p, ACK, 1)
    space = f"@stage(node={c})" if staged else "@dram"
    name = f"p9_{p}{c}_{'stage' if staged else 'dram'}_w{k}_{mode}"
    params = f"y: u64[{2 * k}] {space}, seen: u64[{2 * k * ROUNDS}] @dram"
    return name, b.fn(name, params), b


def fanin(c: int, sources: list[int]):
    b = Body(ledger=True)
    k = MANY_WORDS
    for i in range(ROUNDS):
        for q, p in enumerate(sources):
            bundle(b, p, k, i, q, 2 * k * q)
            b.ring(p, c, SOURCE + q)
        for q, p in enumerate(sources):
            b.take(c, SOURCE + q, 1)
            first = 2 * k * (len(sources) * i + q)
            b.observe(c, "y", range(2 * k * q, 2 * k * (q + 1)), "seen", first)
        for p in sources:
            b.ring(c, p, ACK)
            b.take(p, ACK, 1)
    tag = "".join(map(str, sources))
    name = f"p9_in{c}_{tag}_stage_w{k}"
    n = 2 * k * len(sources)
    params = f"y: u64[{n}] @stage(node={c}), seen: u64[{n * ROUNDS}] @dram"
    return name, b.fn(name, params), b


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD, {}
    routes = list(itertools.permutations(sysnodes, 2))
    cases = [(r, k, True, "now") for r in routes for k in WORDS]
    cases += [(r, k, False, "now") for r in routes for k in DRAM_WORDS]
    cases += [(r, MANY_WORDS, True, m) for r in routes for m in ("late", "early")]
    for (p, c), k, staged, mode in cases:
        name, src, b = pair(p, c, k, staged, mode)
        text += src
        specs[name] = Spec(
            ("y", "seen", "slots"),
            reference([p], k),
            ledger=b.ledger_of(),
            diagnostic=mode == "early",
        )
    for c in sysnodes:
        others = nearest(c, sysnodes)
        for n in range(2, len(others) + 1):
            sources = sorted(others[:n])
            name, src, b = fanin(c, sources)
            text += src
            specs[name] = Spec(
                ("y", "seen", "slots"),
                reference(sources, MANY_WORDS),
                ledger=b.ledger_of(),
            )
    return Module(text, specs)
