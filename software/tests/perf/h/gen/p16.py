"""P16: the dependency hop. One useful chain: the first sysnode's final writer (vector
core, mover or cluster) writes stage 0 of y; each hop's sysnode waits for it (a
fenced ring from the previous sysnode, or the unit's own completion when the hop
stays on one sysnode), copies the stage's first and last words to `seen` right
after that wait, then issues the dependent work: a mover copy of the stage into the
next. Every stage of y equals stage 0, so a hop released early reads POISON. Chains
of 1/2/4/8 hops, so the slope a hop is fit within one chain kind, never a serial
chain minus a dependency-free graph.

Kinds: local (every hop on one sysnode, waits on the unit only); one hop over every
ordered pair (hops 1-3) for each writer; relays around the sysnodes forward from the
first and backward from the last; one hop beside a vector-core stream on both ends
(P1's loaded case); the pre-ready control (the consumer settles first, so its slot
already holds the token when it waits); and the empty control (the same rings, no
work). Every fn with a ring carries its token ledger.

Admission skew: every sysnode's same copy, started by its package (host order) or
held at an on-card GO (the lead gathers a ring from each sysnode, then rings each;
the lead rotates). The host enqueue span is the runner's (its wall per run); the
device start skew is each sysnode's cycles and steps against the bracketed offsets.
BLOCKED(host submit order): `card.run` submits the sysnodes' packages in its own
fixed order, so a fn cannot rotate host order; the GO variants rotate the lead."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.stack.buffer import Buffer

#: A stage's bytes and words (a word is 32 B); the u64 words a hop observes.
STAGE, WORD = 8192, 32
WORDS = STAGE // WORD
OBSERVED = (0, STAGE // 8 - 1)
HOPS = (1, 2, 4, 8)
WRITERS = ("vc", "mover", "mg")
LATE = 20_000
SEEN, ACK, GO = 1, 2, 3
#: The cluster writer's tile (64 x 64, K = 64) as its drain lays it out.
TILE = "f16[64, 64] @dram(tile=16x16)"
LAYOUT_BOARD = "multimesh_v9t6"
MG_PARAMS = "x: mx7a[64, 64] @dram(gm=16, nk=2), w: mx7b[64, 64] @dram(gn=16, nk=2)"
#: The loaded case's stream: vector fills of 2 KiB over 8 MiB on vc[1].
FILLS, FILL = 4096, 2048

IMAGES = f"""
image cp(x, y)
    desc a0 = x walk=(32, {WORDS})
    desc a1 = y walk=(32, {WORDS})
    vfill a0 -> l1[0]
    vsync drain on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt

image load(n, x)
    desc a0 = x walk=(32, {FILL // 32})
    ainc a0 += {FILL}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync fe on=fill
    halt
"""


def copy(b: Body, n: int, src: str, dst: str) -> None:
    walk = f"({WORDS}, 32)"
    b.add(n, "move", f"    copy w({src}, {walk}) -> w({dst}, {walk})")


def write(b: Body, n: int, writer: str) -> None:
    """Stage 0 of y by `writer`, complete when this returns on `n`."""
    if writer == "vc":
        b.add(n, "send vc[0]", "    run cp(x, y)", "wait vc[0]")
    if writer == "mover":
        copy(b, n, "x", "y")
    if writer == "mg":
        b.add(
            n,
            "send mg[0]",
            "    fill A0 <- x n=32",
            "    fill B0 <- w n=32",
            "    gemm 16x16x2 a=A0 b=B0 acc=0",
            f"    drain y n={WORDS}",
            "wait mg[0]",
        )


def chain(route: list[int], writer: str, mode: str):
    """`route[i]` runs hop i (`route[0]` writes stage 0). Modes: now, preready (each
    consumer settles before its wait), loaded (vc[1] streams on every sysnode) and
    empty (the rings alone)."""
    b = Body(ledger=True)
    empty = mode == "empty"
    used = sorted(set(route))
    if mode == "loaded":
        for n in used:
            b.add(n, "send vc[1]", f"    run load({FILLS}, l)")
    if not empty:
        write(b, route[0], writer)
    for i, (p, c) in enumerate(itertools.pairwise(route)):
        if p != c:
            b.ring(p, c, SEEN)
            if mode == "preready":
                b.add(c, f"settle {LATE}")
            b.take(c, SEEN, 1)
        if empty:
            continue
        b.observe(c, f"y + {i * STAGE}", OBSERVED, "seen", 2 * i)
        copy(b, c, f"y + {i * STAGE}", f"y + {(i + 1) * STAGE}")
    if route[-1] != route[0]:
        b.ring(route[-1], route[0], ACK)
        b.take(route[0], ACK, 1)
    if mode == "loaded":
        for n in used:
            b.add(n, "wait vc[1]")
    hops = len(route) - 1
    name = f"p16_{''.join(map(str, route))}_{writer if not empty else 'none'}_{mode}"
    if empty:
        return name, b.fn(name, "unused: u64[4] @dram"), b
    x = MG_PARAMS if writer == "mg" else f"x: f16[{STAGE // 2}] @dram"
    params = (
        f"{x}, y: f16[{(hops + 1) * STAGE // 2}] @dram, " f"seen: f16[{8 * hops}] @dram"
    )
    if mode == "loaded":
        params += f", l: f16[{FILLS * FILL // 2}] @dram"
    return name, b.fn(name, params), b


def staged(hops: int, writer: str):
    """y: stage 0 again in every stage; seen: its observed words, a hop each."""

    def want(ins: dict) -> dict:
        if writer == "mg":
            tile = value_fp16(ins["x"]) @ value_fp16(ins["w"]).T
            json = (MACHINES / f"{LAYOUT_BOARD}.json").read_text("utf-8")
            first = np.frombuffer(Buffer.of(TILE, json).pack(tile), np.float16)
        else:
            first = ins["x"]
        seen = first.view(np.uint64)[list(OBSERVED)].view(np.float16)
        return {"y": np.tile(first, hops + 1), "seen": np.tile(seen, hops)}

    return want


def admission(sysnodes, lead: int | None):
    """Every sysnode copies its x stage to its y stage; `lead` None: started by its
    package, else held at the GO `lead` gathers and rings."""
    b = Body(ledger=lead is not None)
    if lead is not None:
        b.barrier([lead, *(n for n in sysnodes if n != lead)], GO)
    for n in sysnodes:
        b.add(
            n,
            "send vc[0]",
            f"    run cp(x + {n * STAGE}, y + {n * STAGE})",
            "wait vc[0]",
        )
    name = "p16_start_host" if lead is None else f"p16_start_go{lead}"
    size = len(sysnodes) * STAGE // 2
    return name, b.fn(name, f"x: f16[{size}] @dram, y: f16[{size}] @dram"), b


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    first = sysnodes[0]
    forward = [sysnodes[i % len(sysnodes)] for i in range(max(HOPS) + 1)]
    backward = [sysnodes[-1 - i % len(sysnodes)] for i in range(max(HOPS) + 1)]
    cases = [([first] * (h + 1), w, "now") for w in WRITERS for h in HOPS]
    cases += [([n, n], w, "now") for n in sysnodes[1:] for w in WRITERS]
    pairs = list(itertools.permutations(sysnodes, 2))
    cases += [([p, c], w, "now") for (p, c), w in itertools.product(pairs, WRITERS)]
    cases += [(forward[: h + 1], "vc", "now") for h in HOPS if h > 1]
    cases += [(backward[: h + 1], "vc", "now") for h in HOPS if h > 1]
    cases += [([first, c], "vc", "loaded") for c in sysnodes[1:]]
    cases += [([first, c], "vc", "preready") for c in sysnodes[1:]]
    cases += [([p, c], "vc", "empty") for p, c in pairs]
    cases += [(forward[: h + 1], "vc", "empty") for h in HOPS if h > 1]
    text, specs = HEAD + IMAGES, {}
    for route, writer, mode in cases:
        name, src, b = chain(route, writer, mode)
        text += src
        if mode == "empty":
            specs[name] = Spec(("slots",), ledger=b.ledger_of())
            continue
        mg = writer == "mg"
        specs[name] = Spec(
            ("y", "seen", "slots"),
            staged(len(route) - 1, writer),
            numeric=("y", "seen") if mg else (),
            inputs=None if mg else host_tags({"x": (STAGE, np.float16, 0)}),
            ledger=b.ledger_of(),
        )
    tagged = host_tags({"x": (len(sysnodes) * STAGE, np.float16, 0)})
    for lead in [None, *sysnodes]:
        name, src, b = admission(sysnodes, lead)
        text += src
        specs[name] = Spec(
            ("y", "slots") if lead is not None else ("y",),
            lambda ins: {"y": ins["x"]},
            inputs=tagged,
            ledger=b.ledger_of() if lead is not None else None,
        )
    return Module(text, specs)
