"""P8: mover copies, every word checked. From each sysnode, x to y in canonical
DRAM (from a sysnode other than 0 that is the mesh-0 spelling: a word a packet over
the interlink) and x to y in each sysnode's staging, 32 B to 1 MiB, aligned and
with an odd head and tail (one word in from each end; the compiler splits it into
the F-001 legal schedule, L1.MOV.019). A copy landing in another mesh is followed
by a fenced ring there, the landing sysnode waiting for it. Then 1, 2 and 4 movers
at once, 1 MiB each after a start barrier, alone or beside a vector-core and a
cluster read stream on each mover's sysnode. x is host tag words of the run's
epoch.

Not here: DRAM in a mesh's own spelling (mesh bits set), a second cache line of the
same bytes that the runner neither places nor reads; staging copies past 64 KiB."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags, poison

KB = 1 << 10
DRAM_SIZES = (32, KB, 2 * KB, 64 * KB, 1024 * KB)
STAGE_SIZES = (32, KB, 2 * KB, 64 * KB)
#: A word's bytes, a copy command's words at most (the stack's move bound).
WORD, MOVE_WORDS = 32, 16384
#: The concurrent movers' bytes each; the read streams beside them.
EACH = 1024 * KB
FILLS, FILL = 2048, 2048
START, LANDED = 1, 2

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


def copies(x: str, y: str, lo: int, hi: int) -> list[str]:
    """Words lo..hi of x to y, cut at multiples of MOVE_WORDS: no two commands
    write parts of one 64-byte line (L1.VER.017)."""
    cuts = [lo, *range((lo // MOVE_WORDS + 1) * MOVE_WORDS, hi, MOVE_WORDS), hi]
    walks = [(a * WORD, f"({b - a}, 32)") for a, b in itertools.pairwise(cuts)]
    return [f"    copy w({x} + {at}, {n}) -> w({y} + {at}, {n})" for at, n in walks]


def odd_reference(size: int):
    """y: x one word in from each end, POISON in the end words."""

    def want(ins: dict) -> dict:
        x = ins["x"]
        y = poison(np.float16, x.size)
        y[16 : x.size - 16] = x[16 : x.size - 16]
        return {"y": y}

    return want


def one(s: int, target: str, size: int, odd: bool):
    """`target`: "dram", or the staging sysnode's number."""
    b = Body()
    words, head = size // WORD, 1 if odd else 0
    b.add(s, "move", *copies("x", "y", head, words - head))
    landing = 0 if target == "dram" else int(target)
    if landing != s:
        b.ring(s, landing, LANDED)
        b.take(landing, LANDED, 1)
    space = "@dram" if target == "dram" else f"@stage(node={target})"
    where = "dram" if target == "dram" else f"stage{target}"
    name = f"p8_{s}_{where}_{size}b{'_odd' if odd else ''}"
    params = f"x: f16[{size // 2}] @dram, y: f16[{size // 2}] {space}"
    return name, b.fn(name, params)


def many(movers: list[int], loaded: bool):
    b = Body()
    b.barrier(movers, START)
    for k, n in enumerate(movers):
        if loaded:
            b.add(n, "send vc[0]", f"    run load({FILLS}, l)", "send mg[0]")
            b.add(
                n,
                *(f"    fill A{i % 2} <- l + {i * 16 * KB} n=128" for i in range(64)),
            )
        lo = k * EACH // WORD
        b.add(n, "t = move posted", *copies("x", "y", lo, lo + EACH // WORD))
        b.add(n, "wait moves t")
    tag = "".join(map(str, movers))
    name = f"p8_movers{tag}{'_loaded' if loaded else ''}"
    size = len(movers) * EACH // 2
    params = f"x: f16[{size}] @dram, y: f16[{size}] @dram"
    if loaded:
        params += f", l: f16[{FILLS * FILL // 2}] @dram"
    return name, b.fn(name, params)


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD + IMAGES, {}

    def add(fn, reference, size: int) -> None:
        nonlocal text
        name, src = fn
        text += src
        tagged = host_tags({"x": (size, np.float16, 0)})
        specs[name] = Spec(("y",), reference, inputs=tagged)

    def same(ins: dict) -> dict:
        return {"y": ins["x"]}

    cases = [("dram", size) for size in DRAM_SIZES]
    cases += [(str(j), size) for j in sysnodes for size in STAGE_SIZES]
    for s, (target, size), odd in itertools.product(sysnodes, cases, (False, True)):
        if odd and size == WORD:
            continue
        add(one(s, target, size, odd), odd_reference(size) if odd else same, size)
    groups = [list(sysnodes[:k]) for k in (1, 2, 4) if k <= len(sysnodes)]
    for movers, loaded in itertools.product(groups, (False, True)):
        add(many(movers, loaded), same, len(movers) * EACH)
    return Module(text, specs)
