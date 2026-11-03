"""P7: vector peer drains into another die's core, every ordered pair (hops 1-3):
31/32/33/63/64/65 words in one burst (a count that is a multiple of 32 is split by
the compiler, L1.VC.010), then 4/64/256 KiB streams of 256-word bursts back to
back, two in flight: the far core takes each (COUNTed) and writes it to y, and its
sysnode rings FREE for each. The 256 KiB stream again with the far sysnode late,
so the source waits on FREE. x is host tag words of the run's epoch; y == x, every
word."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags

#: One-burst word counts, and the streams' words (4, 64, 256 KiB).
WORDS = (31, 32, 33, 63, 64, 65)
STREAMS = (128, 2048, 8192)
#: A burst's words at most (half a core's 512 L1 words), a word's bytes.
CHUNK, WORD = 256, 32
#: The late far sysnode's delay, in cycles.
LATE = 50_000
START, FREE = 1, 2


def images(meshes) -> str:
    """`take`, and a `put` into each mesh's vc[0] through this mesh's port (0, 1).
    A remote burst names its ack destination (the far core itself), else the
    sending die's interlink raises ACK0 (memory-protocol.md §9)."""
    text = """
image take(n, at, y)
    desc a0 = y walk=(32, n)
    vwait drain rx n=n
    vdrain a0 <- l1[at]
    halt
"""
    for m in meshes:
        text += f"""
image put{m}(n, at, x)
    desc a0 = x walk=(32, n)
    desc a1 = peer(at, ack=mesh{m}.vc[0], count=1, fin=mesh{m}.vc[0]) walk=(32, n)
    vfill a0 -> l1[at]
    vsync drain on=fill
    vdrain a1 <- l1[at] to=(0, 1)
    vsync fe on=drain
    halt
"""
    return text


def stream(s: int, d: int, words: int, late: bool):
    chunk = min(words, CHUNK)
    chunks = words // chunk
    b = Body()
    b.barrier(sorted((s, d)), START)
    if late:
        b.add(d, f"settle {LATE}")
    for i in range(chunks):
        at, off = (i % 2) * chunk, i * chunk * WORD
        if i >= 2:
            b.take(s, FREE, 1)
        b.add(s, "send vc[0]", f"    run put{d}({chunk}, {at}, x + {off})")
        b.add(d, "send vc[0]", f"    run take({chunk}, {at}, y + {off})", "wait vc[0]")
        b.ring(d, s, FREE)
    b.add(s, "wait vc[0]")
    b.take(s, FREE, min(chunks, 2))
    size = f"w{words}" if words < CHUNK else f"{words * WORD >> 10}k"
    name = f"p7_{s}{d}_{size}{'_late' if late else ''}"
    elems = words * WORD // 2
    return name, b.fn(name, f"x: f16[{elems}] @dram, y: f16[{elems}] @dram")


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD + images(sysnodes), {}
    cases = [(w, False) for w in WORDS + STREAMS] + [(STREAMS[-1], True)]
    for (s, d), (words, late) in itertools.product(
        itertools.permutations(sysnodes, 2), cases
    ):
        name, src = stream(s, d, words, late)
        text += src
        specs[name] = Spec(
            ("y",),
            lambda ins: {"y": ins["x"]},
            inputs=host_tags({"x": (words * WORD, np.float16, 0)}),
        )
    return Module(text, specs)
