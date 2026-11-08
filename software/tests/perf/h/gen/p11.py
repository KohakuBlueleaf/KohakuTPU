"""P11: one 32-byte line from a producer sysnode to a consumer, written by the
producer's CPU (four stores of tag words), vector core or mover (from x, host tag
words of the run's epoch), then a ring; the consumer's CPU loads the line right
after its wait and copies it to z, before its ack. ROUNDS rounds a package, new
data each round, into the same line (warm after the first) or a new line each
round (cold, 64 KiB apart: the same stripe); the line in stripe k = 0..3 of the
buffer's base (k = 0 for every ordered pair, 1-3 from the first sysnode).

Modes: now; the producer late (it settles before each write, so a ring that does
not wait for the write's completion releases a stale read); the consumer late (it
settles before each wait); and the notify-before-data admission control (a plain
ring before the write), which requires L1.VER.009 and is never issued. A stale
read shows in z. The mover rewriting one line writes its whole 64-byte DRAM
line: two moves writing part of one line are refused by the checker's ownership
(L1.VER.017). Every fn carries its token ledger (faults included)."""

import itertools

import numpy as np

from h import check
from h.gen import Module, Spec
from h.gen.body import HEAD, Body, dram_at, host_tags, nearest

ROUNDS = 8
WRITERS = ("cpu", "vc", "mover")
MODES = ("now", "late_producer", "late_consumer", "early")
STRIPE, APART, WORD = 16 << 10, 64 << 10, 32
LATE = 20_000
START, SEEN, ACK = 1, 2, 3

IMAGES = """
image line(src, dst)
    desc a0 = src walk=(32, 1)
    desc a1 = dst walk=(32, 1)
    vfill a0 -> l1[0]
    vsync drain on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt
"""


def write(
    b: Body, p: int, writer: str, i: int, at: int, whole: bool, board: str
) -> None:
    """`at`: the line's byte offset in y; `whole`: the mover writes its 64 bytes."""
    if writer == "cpu":
        b.stores(p, dram_at("y", p, board), range(at // 8, at // 8 + 4), i, p, 0)
    if writer == "vc":
        b.add(p, "send vc[0]", f"    run line(x + {WORD * i}, y + {at})", "wait vc[0]")
    if writer == "mover":
        n = 2 if whole else 1
        walk = f"({n}, 32)"
        b.add(
            p,
            "move local=1",
            f"    copy w(x + {WORD * i}, {walk}) -> w(y + {at}, {walk})",
        )


def line(p: int, c: int, writer: str, k: int, fresh: bool, mode: str, board: str):
    b = Body(ledger=True)
    b.barrier(sorted((p, c)), START)
    for i in range(ROUNDS):
        at = k * STRIPE + (i * APART if fresh else 0)
        if mode == "early":
            b.ring(p, c, SEEN, plain=True)
        if mode == "late_producer":
            b.add(p, f"settle {LATE}")
        write(b, p, writer, i, at, not fresh, board)
        if mode != "early":
            b.ring(p, c, SEEN)
        if mode == "late_consumer":
            b.add(c, f"settle {LATE}")
        b.take(c, SEEN, 1)
        b.observe(c, "y", range(at // 8, at // 8 + 4), "z", 4 * i)
        b.ring(c, p, ACK)
        b.take(p, ACK, 1)
    form = "fresh" if fresh else "same"
    name = f"p11_{p}{c}_{writer}_s{k}_{form}_{mode}"
    y = f"y: u64[{(k * STRIPE + ROUNDS * APART) // 8}] @dram"
    if writer == "cpu":
        return name, b.fn(name, f"{y}, z: u64[{4 * ROUNDS}] @dram"), b
    x = f"x: f16[{16 * (ROUNDS + 1)}] @dram"
    return name, b.fn(name, f"{x}, {y}, z: f16[{16 * ROUNDS}] @dram"), b


def stored(p: int, k: int, fresh: bool):
    """z of the CPU writer: each round's four tag words."""

    def want(_: dict) -> dict:
        z = [
            check.tags(
                i, p, 0, (k * STRIPE + (i * APART if fresh else 0)) // 8 + np.arange(4)
            )
            for i in range(ROUNDS)
        ]
        return {"z": np.concatenate(z)}

    return want


def copied(ins: dict) -> dict:
    return {"z": ins["x"][: 16 * ROUNDS]}


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    cases = [
        (p, c, writer, 0, fresh, mode)
        for (p, c), writer, fresh, mode in itertools.product(
            itertools.permutations(sysnodes, 2), WRITERS, (False, True), MODES
        )
    ]
    first = sysnodes[0]
    cases += [
        (first, c, writer, k, False, "now")
        for c, writer, k in itertools.product(
            nearest(first, sysnodes), WRITERS, (1, 2, 3)
        )
    ]
    text, specs = HEAD + IMAGES, {}
    tagged = host_tags({"x": (WORD * (ROUNDS + 1), np.float16, 1)})
    for p, c, writer, k, fresh, mode in cases:
        name, src, b = line(p, c, writer, k, fresh, mode, board)
        text += src
        cpu = writer == "cpu"
        specs[name] = Spec(
            ("z", "slots"),
            stored(p, k, fresh) if cpu else copied,
            inputs=None if cpu else tagged,
            ledger=b.ledger_of(),
            refused="L1.VER.009" if mode == "early" else "",
        )
    return Module(text, specs)
