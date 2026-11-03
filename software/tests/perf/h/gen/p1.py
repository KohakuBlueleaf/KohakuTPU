"""P1: doorbell ping-pong between every ordered pair of sysnodes (hops 1-3), fenced
and plain, waiting on the signal slot or on the doorbell count, idle or beside a
vector-core or mover stream on both sides; and 2 or 3 sources into one sink. The
pong side (the sink) rings READY first, so every side starts its rounds with all
sides up; each side stores its round count. Package vs dispatch-engine path is the
runner's boot flag (`--flags 0x10`), not a fn. Every fn carries its token ledger
(`h.ledger`): each die's ring counts and every slot's residue at the end."""

import itertools

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, counts, nearest
from h.ledger import Ledger

#: Round trips a package; the signal slots; a source's slot at the sink is SOURCE + it.
ROUNDS = (1, 64)
READY, PING, SOURCE = 1, 2, 4
#: The loaded fns' rounds, and the streams beside them: vector fills of 2 KiB over
#: 8 MiB, and a posted mover copy of 1 MiB a side.
LOADED = 64
FILLS, FILL = 4096, 2048
COPY_WORDS = 16384
LOAD_PARAMS = (
    f", x: f16[{FILLS * FILL // 2}] @dram, y: f16[{4 * COPY_WORDS * 16}] @dram"
)

IMAGES = f"""
image load(n, x)
    desc a0 = x walk=(32, {FILL // 32})
    ainc a0 += {FILL}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync fe on=fill
    halt
"""


def stream(b: Body, n: int, load: str, side: int) -> None:
    """`load` started on sysnode `n` and left running through its rounds."""
    if load == "cu":
        b.add(n, "send vc[0]", f"    run load({FILLS}, x)")
    if load == "mover":
        walk, half = f"({COPY_WORDS}, 32)", COPY_WORDS * 32
        at = side * 2 * half
        b.add(
            n,
            "t = move posted",
            f"    copy w(x, {walk}) -> w(y + {at}, {walk})",
            f"    copy w(x + {half}, {walk}) -> w(y + {at + half}, {walk})",
        )


def loop(b: Body, n: int, rounds: int, steps) -> None:
    b.add(n, "r1 = set 0", f"r2 = set {rounds}", "label trip")
    steps()
    b.add(n, "r1 = add r1, 1", "goto trip if r1 < r2")


def pair(i: int, j: int, rounds: int, plain: bool, wait: str, load: str):
    """i pings, j pongs; each side's waits are on the slot or on the bell count."""
    b = Body(bells=True, ledger=True)
    for side, n in enumerate((i, j)):
        stream(b, n, load, side)

    def take(n: int, peer: int, slot: int, times: int) -> None:
        if wait == "slot":
            b.take(n, slot, 1)
        else:
            b.bell(n, peer, 1, times=times)

    b.ring(j, i, READY)
    take(i, j, READY, 1)

    def ping() -> None:
        b.ring(i, j, PING, plain=plain, times=rounds)
        take(i, j, PING, rounds)

    def pong() -> None:
        take(j, i, PING, rounds)
        b.ring(j, i, PING, plain=plain, times=rounds)

    loop(b, i, rounds, ping)
    loop(b, j, rounds, pong)
    b.add(i, "store count + 0 <- r1")
    b.add(j, "store count + 8 <- r1")
    if wait == "bell":
        b.take(i, READY, 1)
        b.take(i, PING, rounds)
        b.take(j, PING, rounds)
    fence = "plain" if plain else "fenced"
    params = "count: u64[2] @dram" + (LOAD_PARAMS if load != "idle" else "")
    name = f"p1_{i}{j}_{fence}_{wait}_{load}_k{rounds}"
    return name, b.fn(name, params), b.ledger_of()


def fanin(sink: int, sources: list[int], rounds: int, plain: bool):
    """Every source pings `sink` on its own slot; the sink answers them in order."""
    b = Body(bells=True, ledger=True)
    for s in sources:
        b.ring(sink, s, READY)
        b.take(s, READY, 1)

    def serve() -> None:
        for s in sources:
            b.take(sink, SOURCE + s, 1)
            b.ring(sink, s, PING, plain=plain, times=rounds)

    loop(b, sink, rounds, serve)
    b.add(sink, "store count + 0 <- r1")
    for k, s in enumerate(sources, 1):

        def ping(s: int = s) -> None:
            b.ring(s, sink, SOURCE + s, plain=plain, times=rounds)
            b.take(s, PING, 1)

        loop(b, s, rounds, ping)
        b.add(s, f"store count + {8 * k} <- r1")
    fence = "plain" if plain else "fenced"
    tag = "".join(map(str, sources))
    name = f"p1_in{sink}_{tag}_{fence}_k{rounds}"
    return name, b.fn(name, f"count: u64[{1 + len(sources)}] @dram"), b.ledger_of()


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD + IMAGES, {}

    def add(fn: tuple[str, str, Ledger], outputs: int, rounds: int) -> None:
        nonlocal text
        name, src, ledger = fn
        text += src
        specs[name] = Spec(
            ("count", "slots"),
            lambda _, k=rounds, n=outputs: counts(k, n),
            ledger=ledger,
        )

    for (i, j), rounds, plain, wait in itertools.product(
        itertools.permutations(sysnodes, 2), ROUNDS, (False, True), ("slot", "bell")
    ):
        add(pair(i, j, rounds, plain, wait, "idle"), 2, rounds)
    for (i, j), plain, load in itertools.product(
        itertools.permutations(sysnodes, 2), (False, True), ("cu", "mover")
    ):
        add(pair(i, j, LOADED, plain, "slot", load), 2, LOADED)
    for sink, rounds, plain in itertools.product(sysnodes, ROUNDS, (False, True)):
        others = nearest(sink, sysnodes)
        for k in range(2, len(others) + 1):
            add(fanin(sink, sorted(others[:k]), rounds, plain), 1 + k, rounds)
    return Module(text, specs)
