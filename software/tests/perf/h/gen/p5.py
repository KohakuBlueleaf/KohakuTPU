"""P5: cluster drains into another die, every ordered pair (hops 1-3) and the two
nearest sources into one die at once, 4 KiB to 1 MiB a source. Each source's
cluster sums one 256 x 256 tile of x w^T (K = 256), then after a start barrier
drains it in chunks of at most 256 sub-tiles across the interlink into its own
core of the destination die, two chunks in flight: the core takes a chunk
(COUNTed) and writes it to y, and the destination sysnode rings FREE for each. A
chunk is the tile's first sub-tiles (a drain starts at sub-tile 0), so y holds
that prefix again and again.

Not here: a drain to the far die's DRAM (`drain ADDR n=N mesh=M`), which the static
checker refuses ("memory drain carries unit destination"); a third source into one
die, which would share a core's port with another source unordered (a `port`
finding): a die has two cores."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, nearest
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.stack.buffer import Buffer

KB = 1 << 10
SIZES = (4 * KB, 16 * KB, 64 * KB, 256 * KB, 1024 * KB)
#: A chunk's sub-tiles at most (half a core's 512 L1 words), a sub-tile's bytes.
CHUNK, SUBTILE = 256, 32
START, FREE = 1, 2
#: The tile's layout as the drain writes it (the same on every v9 board).
TILE = "f16[256, 256] @dram(tile=64x64)"
LAYOUT_BOARD = "multimesh_v9t6"

IMAGES = """
image take(n, at, y)
    desc a0 = y walk=(32, n)
    vwait drain rx n=n
    vdrain a0 <- l1[at]
    halt
"""

GEMM = [
    "send mg[0]",
    "    for c in 0..4",
    "        fill A{c % 2} <- x + c*16K n=128",
    "        fill B{c % 2} <- w + c*16K n=128",
    "        gemm 64x64x2 a=A{c % 2} b=B{c % 2} acc=(c > 0)",
    "wait mg[0]",
]


def reference(sources: int, size: int):
    """y: each source's chunks of the tile's leading sub-tiles."""
    chunk = min(size // SUBTILE, CHUNK)

    def want(ins: dict) -> dict:
        tile = value_fp16(ins["x"]) @ value_fp16(ins["w"]).T
        json = (MACHINES / f"{LAYOUT_BOARD}.json").read_text("utf-8")
        head = Buffer.of(TILE, json).pack(tile)[: chunk * SUBTILE]
        reps = sources * size // (chunk * SUBTILE)
        return {"y": np.frombuffer(head * reps, np.float16)}

    return want


def drains(sources: list[int], d: int, size: int):
    chunk = min(size // SUBTILE, CHUNK)
    chunks = size // (chunk * SUBTILE)
    b = Body()
    for s in sources:
        b.add(s, *GEMM)
    b.barrier(sorted([d, *sources]), START)
    for i in range(chunks):
        at = (i % 2) * chunk
        for q, s in enumerate(sources):
            if i >= 2:
                b.take(s, FREE, 1)
            b.add(
                s,
                "send mg[0]",
                f"    drain n={chunk} to=mesh{d}.vc[{q}] buf=0 l1={at} count",
            )
            y = f"y + {q * size + i * chunk * SUBTILE}"
            b.add(d, f"send vc[{q}]", f"    run take({chunk}, {at}, {y})")
        for q, s in enumerate(sources):
            b.add(d, f"wait vc[{q}]")
            b.ring(d, s, FREE)
    for s in sources:
        b.add(s, "wait mg[0]")
        b.take(s, FREE, min(chunks, 2))
    tag = "".join(map(str, sources))
    name = f"p5_{tag}to{d}_{size // KB}k"
    params = (
        "x: mx7a[256, 256] @dram(gm=64, nk=2), w: mx7b[256, 256] @dram(gn=64, nk=2), "
        f"y: f16[{len(sources) * size // 2}] @dram"
    )
    return name, b.fn(name, params)


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD + IMAGES, {}
    groups = [([s], d) for d, s in itertools.permutations(sysnodes, 2)]
    if len(sysnodes) > 2:
        groups += [(sorted(nearest(d, sysnodes)[:2]), d) for d in sysnodes]
    for (sources, d), size in itertools.product(groups, SIZES):
        name, src = drains(sources, d, size)
        text += src
        specs[name] = Spec(("y",), reference(len(sources), size), numeric=("y",))
    return Module(text, specs)
