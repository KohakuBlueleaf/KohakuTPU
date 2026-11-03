"""P18, firmware model A: retained graphs as four resident sysnode packages, replayed
R = 1/8/64/512 times inside each package (an L1 loop over the whole graph). Each
replay opens with an on-card GO (the first sysnode gathers a ring from every other
and rings each back) and closes with a terminal gather (every other rings the first,
which takes them all), so every token is drained before the next replay and the
ledger proves no slot is left holding one. A segment is a mover copy of one 1 KiB
chunk of x (host tag words of the run's epoch); an edge is a fenced ring after the
producer's segment completes; its consumer copies the chunk's first and last words
to `seen` right after its wait, then copies the chunk to z (its dependent work).

Graphs, 1/4/16/64 segments or edges: empty (GO and gather only); shards and join
(every sysnode its segments, the first joins: it takes each shard's ring, observes
each shard's last chunk, then copies all of y to z); chain (edge k from sysnode k to
k + 1 around the line, each copying the previous stage on); fan-out (the first
sysnode's segment k to the others in turn) and fan-in (the others' segments into
the first). Size 4 also at R = 8/64/512, and with the last consumer late.

Receiver ready before payload: the receiver arms its vector core's counted receive
of chunk k, then its CPU rings READY (the message that enables its producer: armed is
not completed, so the ring precedes its wait), then waits for the landing and
observes. The producer waits READY, then drains the chunk into the receiver's core
(P7's peer drain). Variants: now; late (the receiver settles before arming, so the
producer is held at READY: no fill into unready storage); progress (the receiver's
vc[1] copies chunk k of x to z between arming and waiting). In a replayed package an
observation is overwritten each replay: `seen` holds the last.

The runner binds and compiles once a fn and sends the packages each run; whether a
run re-uploads them is the runtime's submit cache (`KOHAKUTPU_HOST_SUBMIT_CACHE`,
in meta's knobs), not witnessed here, so retention across runs is not claimed: the
in-package replays are the retained replays. Device phases come from each
sysnode's cycles and steps; host compile/bind time is the runner's, never a phase.
BLOCKED(quiescent rebind/unmap): no firmware model A step invalidates a bound
capability, so old-binding refusal after rebind is not expressible. BLOCKED(install
admission): bind and install are one host step per run; a descriptor/effect
mismatch is refused only by the compile-time static checker (test_gen's refusals).
BLOCKED(KAOS segment backend): Q0-KAOS has not qualified it."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags
from h.gen.p7 import images as peer_images

#: A chunk's bytes and words, and its first and last u64 words.
CHUNK, WORD = 1024, 32
WORDS = CHUNK // WORD
SIZES = (1, 4, 16, 64)
REPLAYS = (1, 8, 64, 512)
GRAPHS = ("shards", "chain", "fanout", "fanin")
LATE = 20_000
GO, END, READY, SOURCE = 1, 2, 3, 4

IMAGES = f"""
image chunk(x, y)
    desc a0 = x walk=(32, {WORDS})
    desc a1 = y walk=(32, {WORDS})
    vfill a0 -> l1[{2 * WORDS}]
    vsync drain on=fill
    vdrain a1 <- l1[{2 * WORDS}]
    vsync fe on=drain
    halt
"""


def ends(k: int) -> list[int]:
    """Chunk k's first and last u64 words."""
    return [k * CHUNK // 8, (k + 1) * CHUNK // 8 - 1]


def segment(b: Body, n: int, src: str, dst: str) -> None:
    walk = f"({WORDS}, 32)"
    b.add(n, "move", f"    copy w({src}, {walk}) -> w({dst}, {walk})")


def opened(b: Body, sysnodes: list[int], r: int) -> None:
    """The GO: the first gathers a ring from each other, then rings each back."""
    lead, *rest = sysnodes
    for n in rest:
        b.ring(n, lead, GO, times=r)
    b.take(lead, GO, len(rest))
    for n in rest:
        b.ring(lead, n, GO, times=r)
        b.take(n, GO, 1)


def closed(b: Body, sysnodes: list[int], r: int) -> None:
    """The terminal gather: every other rings the first, which takes them all."""
    lead, *rest = sysnodes
    for n in rest:
        b.ring(n, lead, END, times=r)
    b.take(lead, END, len(rest))


def replayed(b: Body, r: int) -> None:
    """Every block's lines inside an L1 loop of `r` trips."""
    if r == 1:
        return
    for n, lines in b.lines.items():
        b.lines[n] = [
            "r1 = set 0",
            f"r2 = set {r}",
            "label rep",
            *lines,
            "r1 = add r1, 1",
            "goto rep if r1 < r2",
        ]


def edge(b: Body, p: int, c: int, k: int, r: int, late: bool, src: str) -> None:
    """p's segment k (from `src` chunk to y chunk k), ring, c's observation and its
    copy of the chunk to z."""
    segment(b, p, src, f"y + {k * CHUNK}")
    b.ring(p, c, SOURCE + p, times=r)
    if late:
        b.add(c, f"settle {LATE}")
    b.take(c, SOURCE + p, 1)
    b.observe(c, "y", ends(k), "seen", 2 * k)
    segment(b, c, f"y + {k * CHUNK}", f"z + {k * CHUNK}")


def graph(sysnodes, kind: str, size: int, r: int, late: bool):
    """Returns the fn's name, text, ledger and reference."""
    nodes = list(sysnodes)
    lead, rest = nodes[0], nodes[1:]
    b = Body(ledger=True)
    opened(b, nodes, r)
    chunks, seen = size, size
    if kind == "shards":
        chunks, seen = size * len(nodes), len(rest)
        for q, n in enumerate(nodes):
            for j in range(q * size, (q + 1) * size):
                segment(b, n, f"x + {j * CHUNK}", f"y + {j * CHUNK}")
            if n != lead:
                b.ring(n, lead, SOURCE + n, times=r)
        for i, n in enumerate(rest, 1):
            b.take(lead, SOURCE + n, 1)
            b.observe(lead, "y", ends((i + 1) * size - 1), "seen", 2 * (i - 1))
        walk = f"({chunks * WORDS}, 32)"
        b.add(lead, "move", f"    copy w(y, {walk}) -> w(z, {walk})")
    for k in range(size if kind != "shards" else 0):
        last = late and k == size - 1
        if kind == "chain":
            p, c = nodes[k % len(nodes)], nodes[(k + 1) % len(nodes)]
            src = "x" if k == 0 else f"z + {(k - 1) * CHUNK}"
            edge(b, p, c, k, r, last, src)
        if kind == "fanout":
            edge(b, lead, rest[k % len(rest)], k, r, last, f"x + {k * CHUNK}")
        if kind == "fanin":
            edge(b, rest[k % len(rest)], lead, k, r, last, f"x + {k * CHUNK}")
    closed(b, nodes, r)
    replayed(b, r)
    name = f"p18_{kind}_s{size}_r{r}{'_late' if late else ''}"
    half = chunks * CHUNK // 2
    params = (
        f"x: f16[{half}] @dram, y: f16[{half}] @dram, z: f16[{half}] @dram, "
        f"seen: u64[{2 * seen}] @dram"
    )

    def want(ins: dict) -> dict:
        x = ins["x"]
        if kind == "chain":
            x = np.tile(x[: CHUNK // 2], chunks)
        at = (
            [ends((i + 1) * size - 1) for i in range(1, len(nodes))]
            if kind == "shards"
            else [ends(k) for k in range(size)]
        )
        words = x.view(np.uint64)
        return {"y": x, "z": x, "seen": words[np.ravel(at)]}

    return name, b.fn(name, params), b, want, chunks


def empty(sysnodes, r: int):
    b = Body(ledger=True)
    opened(b, list(sysnodes), r)
    closed(b, list(sysnodes), r)
    replayed(b, r)
    name = f"p18_empty_r{r}"
    return name, b.fn(name, "unused: u64[4] @dram"), b


def ready(p: int, d: int, size: int, r: int, mode: str):
    """`size` chunks from p into d's core, each armed by d before its READY."""
    b = Body(ledger=True)
    opened(b, [p, d], r)
    for k in range(size):
        if mode == "late":
            b.add(d, f"settle {LATE}")
        b.add(d, "send vc[0]", f"    run take({WORDS}, 0, y + {k * CHUNK})")
        b.ring(d, p, READY, times=r)
        if mode == "progress":
            b.add(d, "send vc[1]", f"    run chunk(x + {k * CHUNK}, z + {k * CHUNK})")
        b.take(p, READY, 1)
        b.add(p, "send vc[0]", f"    run put{d}({WORDS}, 0, x + {k * CHUNK})")
        b.add(p, "wait vc[0]")
        b.add(d, "wait vc[0]")
        b.observe(d, "y", ends(k), "seen", 2 * k)
        if mode == "progress":
            b.add(d, "wait vc[1]")
    closed(b, [p, d], r)
    replayed(b, r)
    name = f"p18_ready_{p}{d}_s{size}_r{r}_{mode}"
    half = size * CHUNK // 2
    params = f"x: f16[{half}] @dram, y: f16[{half}] @dram, seen: u64[{2 * size}] @dram"
    if mode == "progress":
        params += f", z: f16[{half}] @dram"

    def want(ins: dict) -> dict:
        x = ins["x"]
        at = np.ravel([ends(k) for k in range(size)])
        out = {"y": x, "seen": x.view(np.uint64)[at]}
        return out | ({"z": x} if mode == "progress" else {})

    outputs = ("y", "seen", "slots") + (("z",) if mode == "progress" else ())
    return name, b.fn(name, params), b, want, outputs


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD + IMAGES + peer_images(sysnodes), {}
    for r in REPLAYS:
        name, src, b = empty(sysnodes, r)
        text += src
        specs[name] = Spec(("slots",), ledger=b.ledger_of())
    cases = [(g, s, 1, False) for g, s in itertools.product(GRAPHS, SIZES)]
    cases += [(g, 4, r, False) for g, r in itertools.product(GRAPHS, REPLAYS[1:])]
    cases += [(g, 4, 1, True) for g in GRAPHS if g != "shards"]
    for kind, size, r, late in cases:
        name, src, b, want, chunks = graph(sysnodes, kind, size, r, late)
        text += src
        specs[name] = Spec(
            ("y", "z", "seen", "slots"),
            want,
            inputs=host_tags({"x": (chunks * CHUNK, np.float16, 0)}),
            ledger=b.ledger_of(),
        )
    first = sysnodes[0]
    runs = [(s, 1, m) for s in SIZES for m in ("now", "late", "progress")]
    runs += [(4, r, "now") for r in REPLAYS[1:]]
    for d, (size, r, mode) in itertools.product(sysnodes[1::2], runs):
        name, src, b, want, outputs = ready(first, d, size, r, mode)
        text += src
        specs[name] = Spec(
            outputs,
            want,
            inputs=host_tags({"x": (size * CHUNK, np.float16, 0)}),
            ledger=b.ledger_of(),
        )
    return Module(text, specs)
