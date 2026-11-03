"""Q0: what the later rows' records and checks mean, on the smallest programs. A
package with no work on each sysnode and on all at once; an exact local mover copy
on each; one ring of amount 1 or 3 and its consume between every ordered pair (its
ledger: one packet received from the source, one sent, the slot left empty); and a
bounded two-part dependency: the producer settles, copies x to y and rings fenced,
the consumer copies y's first and last words to `seen` right after its wait, copies
y to z and acks. Inputs are host tag words of the run's epoch.

The runner supplies the rest of Q0: the checker's mutation controls before any run
(`check.sensitivity`), the bracketed cycle offsets and their drift around every fn
(`meta.timebase`), the measure records' count and loss with `--measure`, and
trace-on/off as a pair of runs differing only in `--flags` (0, STEPS+TIMING)."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags

#: The copies' bytes (a word is 32 B), and the producer's settle.
BYTES, WORD = 4096, 32
DELAY = 20_000
AMOUNTS = (1, 3)
SLOT, ACK = 1, 2
#: The last u64 word of a copy.
LAST = BYTES // 8 - 1


def empty(sysnodes) -> tuple[str, str]:
    tag = "".join(map(str, sysnodes))
    name = f"q0_empty_{tag}"
    blocks = "".join(f"\n    node {n}" for n in sysnodes)
    return name, f"\nfn {name}.l1(unused: u64[4] @dram){blocks}\n"


def copy(b: Body, n: int, src: str, dst: str) -> None:
    walk = f"({BYTES // WORD}, 32)"
    b.add(n, "move", f"    copy w({src}, {walk}) -> w({dst}, {walk})")


def local(n: int) -> tuple[str, str]:
    b = Body()
    copy(b, n, "x", "y")
    name = f"q0_copy_{n}"
    return name, b.fn(name, f"x: f16[{BYTES // 2}] @dram, y: f16[{BYTES // 2}] @dram")


def ring(s: int, d: int, amount: int):
    b = Body(ledger=True)
    b.ring(s, d, SLOT, amount=amount)
    b.take(d, SLOT, amount)
    name = f"q0_ring_{s}{d}_a{amount}"
    return name, b.fn(name, "unused: u64[4] @dram"), b


def dependency(s: int, d: int):
    b = Body(ledger=True)
    b.add(s, f"settle {DELAY}")
    copy(b, s, "x", "y")
    b.ring(s, d, SLOT)
    b.take(d, SLOT, 1)
    b.observe(d, "y", [0, LAST], "seen", 0)
    copy(b, d, "y", "z")
    b.ring(d, s, ACK)
    b.take(s, ACK, 1)
    name = f"q0_dep_{s}{d}"
    half = BYTES // 2
    params = f"x: f16[{half}] @dram, y: f16[{half}] @dram, z: f16[{half}] @dram"
    return name, b.fn(name, params + ", seen: u64[2] @dram"), b


def copied(ins: dict) -> dict:
    x = ins["x"]
    return {"y": x, "z": x, "seen": x.view(np.uint64)[[0, LAST]]}


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD, {}
    tagged = host_tags({"x": (BYTES, np.float16, 0)})
    for group in [(n,) for n in sysnodes] + [sysnodes]:
        name, src = empty(group)
        text += src
        specs[name] = Spec(())
    for n in sysnodes:
        name, src = local(n)
        text += src
        specs[name] = Spec(("y",), lambda ins: {"y": ins["x"]}, inputs=tagged)
    for (s, d), amount in itertools.product(
        itertools.permutations(sysnodes, 2), AMOUNTS
    ):
        name, src, b = ring(s, d, amount)
        text += src
        specs[name] = Spec(("slots",), ledger=b.ledger_of())
    for s, d in itertools.permutations(sysnodes, 2):
        name, src, b = dependency(s, d)
        text += src
        specs[name] = Spec(
            ("y", "z", "seen", "slots"), copied, inputs=tagged, ledger=b.ledger_of()
        )
    return Module(text, specs)
