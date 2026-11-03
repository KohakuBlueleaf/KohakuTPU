"""C3: C1's compute with checked interlink streams running at the same time, on
reserved units: the compute keeps to vc[1] and mg[0], a stream to vc[0] (its
receiver) and to vc[0] or mg[1] (its sender), so no receive context is shared.
After a start barrier every participating sysnode interleaves its compute steps
with its stream steps in one block (the CPU feeds both), each unit running
asynchronously; a token ledger checks every ring and every die's faults (a
remote burst names its ack destination, so ACK0 is a fault).

Compute, on every sysnode: `vc` (vc[1] unpacks, doubles and packs 256 KiB of host
tag words in 2 KiB trips: C1's checked pack, streamed) or `mgvc` (16 of C1's
mg[0] -> vc[1] tiles, one after another), or `none` (the link-only control).
Streams (256 KiB each, 8 KiB bursts, two in flight, the receiver ringing FREE for
each as P5 / P7): `p7` (vc[0] peer drains into the far vc[0], x host tag words)
and `p5` (mg[1] drains one exact-witness 256 x 256 tile's first 8 KiB into the
far vc[0], again and again), or `none` (the idle-link control). Routes on the
line 0-1-2-3: `one` 0 -> 1; `disjoint` 0 -> 1 and 2 -> 3 (edge-disjoint);
`trunk` 0 -> 2 and 1 -> 3 (both on link 1-2, equal bytes); `opposite` 0 -> 1 and
1 -> 0 (p5 only: a vc[0] cannot send and receive at once). Every output is
compared word for word.

Overlap evidence: each run's row holds every sysnode's cycles and (with the
STEPS|TIMING boot flags, `--flags 0xa`) its step stamps, so a compute step's and
a stream step's intervals on one sysnode's clock show the overlap; the interlink
deltas give each link's packets, stalls and forwarding during the run. The
`none` fns are the matched controls (same steps, same bindings shape)."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags
from h.gen.c1 import TAKE_TEXT, TILE, gemm, matmul_params, operands, product
from h.gen.p5 import LAYOUT_BOARD
from h.gen.p7 import images as peer_images
from h.gen.p13 import witness
from h.gen.p19 import IMAGES as DBL
from h.gen.p19 import PACK_WORDS
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.stack.buffer import Buffer

KB = 1 << 10
#: A stream's bytes and burst words; the vector compute's trips.
STREAM, BURST, WORD = 256 * KB, 256, 32
BURSTS = STREAM // (BURST * WORD)
TRIPS = STREAM // (PACK_WORDS * WORD)
#: The matmul compute's tiles (each 4 KiB of q).
TILES = 16
#: The p5 source tile: 256 x 256, K = 256, 64 x 64 sub-tiles, K chunks of 64.
EDGE, SRC_CHUNKS = 256, 4
START, FREE = 1, 2
ROUTES = {
    "one": ((0, 1),),
    "disjoint": ((0, 1), (2, 3)),
    "trunk": ((0, 2), (1, 3)),
    "opposite": ((0, 1), (1, 0)),
}


def source_tile(ins: dict, s: int) -> np.ndarray:
    """The p5 sender's tile in its drain layout, its first burst as f16."""
    json = (MACHINES / f"{LAYOUT_BOARD}.json").read_text("utf-8")
    y = ins[f"px{s}"].astype(np.float64) @ ins[f"pw{s}"].astype(np.float64).T
    raw = Buffer.of(f"f16[{EDGE}, {EDGE}] @dram(tile=64x64)", json).pack(y)
    return np.frombuffer(raw[: BURST * WORD], np.float16)


def stream(b: Body, kind: str, s: int, d: int) -> tuple[list, list]:
    """The sender's and the receiver's steps (callables, in order)."""

    def send(i: int):
        def step() -> None:
            if i >= 2:
                b.take(s, FREE, 1)
            at, off = (i % 2) * BURST, i * BURST * WORD
            if kind == "p7":
                b.add(
                    s, "send vc[0]", f"    run put{d}({BURST}, {at}, x{s}{d} + {off})"
                )
                return
            b.add(
                s,
                "send mg[1]",
                f"    drain n={BURST} to=mesh{d}.vc[0] buf=0 l1={at} count "
                f"ack=mesh{d}.vc[0]",
            )

        return step

    def take(i: int):
        def step() -> None:
            at, off = (i % 2) * BURST, i * BURST * WORD
            b.add(d, "send vc[0]", f"    run take({BURST}, {at}, l{s}{d} + {off})")
            b.add(d, "wait vc[0]")
            b.ring(d, s, FREE)

        return step

    def opening() -> None:
        if kind == "p5":
            lines = ["send mg[1]"]
            for c in range(SRC_CHUNKS):
                lines += [
                    f"    fill A{c % 2} <- px{s} + {c * 16 * KB} n=128",
                    f"    fill B{c % 2} <- pw{s} + {c * 16 * KB} n=128",
                    f"    gemm 64x64x2 a=A{c % 2} b=B{c % 2} acc={int(c > 0)}",
                ]
            b.add(s, *lines)

    def closing() -> None:
        b.add(s, "wait vc[0]" if kind == "p7" else "wait mg[1]")
        b.take(s, FREE, min(BURSTS, 2))

    sender = [opening, *(send(i) for i in range(BURSTS)), closing]
    return sender, [take(i) for i in range(BURSTS)]


def compute(b: Body, kind: str, n: int) -> tuple[list, list]:
    """The compute's steps, and its closing steps."""
    if kind == "vc":
        return [lambda: b.add(n, "send vc[1]", f"    run dbl({TRIPS}, u{n}, v{n})")], [
            lambda: b.add(n, "wait vc[1]")
        ]
    if kind == "mgvc":
        return [
            (lambda t=t: gemm(b, n, f"a{n}", f"w{n}", f"q{n} + {t * TILE * WORD}"))
            for t in range(TILES)
        ], []
    return [], []


def fn(sysnodes, work: str, link: str, route: str):
    pairs = [(sysnodes[s], sysnodes[d]) for s, d in ROUTES[route]] if link else []
    ends = sorted({n for p in pairs for n in p})
    party = list(sysnodes) if work != "none" else ends
    b = Body(ledger=True)
    b.barrier(party, START)
    steps = {n: [] for n in party}
    tails = {n: [] for n in party}
    for n in party:
        main, tail = compute(b, work, n)
        steps[n].append(main)
        tails[n] += tail
    takes = {n: [] for n in party}
    for s, d in pairs:
        sender, receiver = stream(b, link, s, d)
        steps[s].append(sender)
        takes[d].append([None, *receiver])
    for n in party:
        steps[n] += takes[n]
        for step in itertools.chain(*itertools.zip_longest(*steps[n])):
            if step is not None:
                step()
        for step in tails[n]:
            step()
    params, given, want = [], {}, {}
    if work == "vc":
        params += [f"u{n}: f16[{STREAM // 2}] @dram" for n in party]
        params += [f"v{n}: f16[{STREAM // 2}] @dram" for n in party]
        given |= host_tags({f"u{n}": (STREAM, np.float16, n) for n in party})(0)
        want |= {f"v{n}": lambda ins, n=n: ins[f"u{n}"] * np.float16(2) for n in party}
    if work == "mgvc":
        for n in party:
            params += [
                *matmul_params(n, "w"),
                f"q{n}: f16[{TILES * 64}, 32] @dram(tile=16x8)",
            ]
            want[f"q{n}"] = lambda ins, n=n: np.tile(product(ins, n, "w"), (TILES, 1))
        given |= operands(party, "w", False)
    for s, d in pairs:
        params.append(f"l{s}{d}: f16[{STREAM // 2}] @dram")
        if link == "p7":
            params.append(f"x{s}{d}: f16[{STREAM // 2}] @dram")
            given |= host_tags({f"x{s}{d}": (STREAM, np.float16, s)})(0)
            want[f"l{s}{d}"] = lambda ins, s=s, d=d: ins[f"x{s}{d}"]
        else:
            if f"px{s}" not in given:
                params += [
                    f"px{s}: mx7a[{EDGE}, {EDGE}] @dram(gm=64, nk=2)",
                    f"pw{s}: mx7b[{EDGE}, {EDGE}] @dram(gn=64, nk=2)",
                ]
                given[f"px{s}"] = witness(EDGE, EDGE, 0, 20 + s)
                given[f"pw{s}"] = witness(EDGE, EDGE, 1, 30 + s)
            want[f"l{s}{d}"] = lambda ins, s=s: np.tile(source_tile(ins, s), BURSTS)
    name = f"c3_{work}_{link}_{route}" if link else f"c3_{work}_none"

    def reference(ins: dict) -> dict:
        return {p: f(ins) for p, f in want.items()}

    spec = Spec(
        (*want, "slots"),
        reference,
        inputs=lambda _: given,
        fresh=False,
        ledger=b.ledger_of(),
    )
    return name, b.fn(name, ", ".join(params)), spec


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    links = [("", "one")]
    for link, route in itertools.product(("p7", "p5"), ROUTES):
        need = max(max(p) for p in ROUTES[route]) < len(sysnodes)
        if need and not (link == "p7" and route == "opposite"):
            links.append((link, route))
    text, specs = HEAD + DBL + TAKE_TEXT + peer_images(sysnodes), {}
    for work, (link, route) in itertools.product(("none", "vc", "mgvc"), links):
        if work == "none" and not link:
            continue
        name, src, spec = fn(sysnodes, work, link, route)
        text += src
        specs[name] = spec
    return Module(text, specs)
