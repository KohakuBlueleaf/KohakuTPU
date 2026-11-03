"""P12: shared all-gather. After a start barrier every sysnode's core writes its
column shard of y (R rows of W bytes, x's shard s into y's columns s W .. (s + 1) W)
in canonical DRAM, then rings each consumer fenced; a consumer waits for every
other producer, then reads its own rows of y (rows) or all of y (all), its core
writing the last read to v. Shards 4 KiB to 64 MiB (153,600 and 614,400 B at
ViT-B's 200 rows); 1, 2 or 4 readers; y's base moved by 64 B steps and by 16 KiB
stripes; producers all starting at row 0 (same) or each at its own quarter
(staggered). Shard s of x is tag words of producer s, written once."""

import itertools
import math

import numpy as np

from h import check
from h.gen import Module, Spec
from h.gen.body import HEAD, Body, poison

KB = 1 << 10
#: (rows, a row's bytes) of one producer's shard.
SHARDS = (
    (8, 512),
    (32, 512),
    (128, 512),
    (200, 768),
    (200, 3072),
    (1024, 4096),
    (8192, 8192),
)
#: y's base offsets for the 153,600 B shard, read by every sysnode by rows.
OFFSETS = (64, 128, 192, 16 * KB, 32 * KB, 48 * KB)
OFFSET_SHARD = (200, 768)
#: A core's words a burst at most (half its L1), a word's bytes.
HALF, WORD = 256, 32
START, DONE = 1, 2


def put_image(width: int, rows: int, pitch: int) -> tuple[str, str]:
    """A core writing `rows` rows of `width` bytes a trip into rows `pitch` apart."""
    name = f"put_{width}_{rows}_{pitch}"
    return (
        name,
        f"""
image {name}(n, src, dst)
    desc a0 = src walk=(32, {rows * width // WORD})
    desc a1 = dst walk=((32, {width // WORD}), ({pitch}, {rows}))
    ainc a0 += {rows * width}
    ainc a1 += {rows * pitch}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync drain on=fill
        vdrain a1 <- l1[0] rel
        vsync fill on=drain
    halt
""",
    )


def get_image(words: int) -> tuple[str, str]:
    """A core reading `words` words a trip, the last trip written to v."""
    name = f"get_{words}"
    return (
        name,
        f"""
image {name}(n, src, v)
    desc a0 = src walk=(32, {words})
    desc a1 = v walk=(32, {words})
    ainc a0 += {words * WORD}
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync fe on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt
""",
    )


def gather(
    sysnodes, shard, readers: int, every_row: bool, offset: int, staggered: bool
):
    """Returns the fn's name, text, images and reference."""
    rows, width = shard
    producers = list(sysnodes)
    consumers = producers[:readers]
    pitch = len(producers) * width
    per = max(
        d for d in range(1, rows + 1) if rows % d == 0 and d * width <= HALF * WORD
    )
    put, put_text = put_image(width, per, pitch)
    b = Body()
    b.barrier(producers, START)
    for s in producers:
        first = s * rows // len(producers) // per * per if staggered else 0
        b.add(s, "send vc[0]")
        for lo, hi in ((first, rows), (0, first)):
            if hi > lo:
                src, dst = (
                    s * rows * width + lo * width,
                    offset + lo * pitch + s * width,
                )
                b.add(s, f"    run {put}({(hi - lo) // per}, x + {src}, y + {dst})")
        b.add(s, "wait vc[0]")
        for c in consumers:
            if c != s:
                b.ring(s, c, DONE)
    share = rows // len(consumers) if every_row else rows
    region = share * pitch
    words = math.gcd(region // WORD, HALF)
    get, get_text = get_image(words)
    for q, c in enumerate(consumers):
        b.take(c, DONE, len(producers) - 1)
        lo = q * share if every_row else 0
        src, v = offset + lo * pitch, q * words * WORD
        run = f"    run {get}({region // (words * WORD)}, y + {src}, v + {v})"
        b.add(c, "send vc[0]", run, "wait vc[0]")
    read = "rows" if every_row else "all"
    order = "_staggered" if staggered else ""
    name = f"p12_{rows}x{width}_r{readers}_{read}_o{offset}{order}"
    size = (offset + rows * pitch) // 2
    params = (
        f"x: f16[{len(producers) * rows * width // 2}] @dram, y: f16[{size}] @dram, "
        f"v: f16[{len(consumers) * words * WORD // 2}] @dram"
    )

    def want(ins: dict) -> dict:
        shards = ins["x"].reshape(len(producers), rows, width // 2)
        full = shards.transpose(1, 0, 2).reshape(-1)
        y = np.concatenate([poison(np.float16, offset // 2), full])
        last = [
            (q * share if every_row else 0) * pitch + region - words * WORD
            for q in range(len(consumers))
        ]
        v = np.concatenate([full[a // 2 : (a + words * WORD) // 2] for a in last])
        return {"y": y, "v": v}

    def tagged(_: int) -> dict:
        shard = [check.tags(0, s, 0, np.arange(rows * width // 8)) for s in producers]
        return {"x": np.concatenate(shard).view(np.float16)}

    images = {put: put_text, get: get_text}
    spec = Spec(("y", "v"), want, inputs=tagged, fresh=False)
    return name, b.fn(name, params), images, spec


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    every = len(sysnodes)
    counts = [k for k in (1, 2, 4) if k <= every]
    cases = [
        (shard, k, every_row, 0, False)
        for shard, k, every_row in itertools.product(SHARDS, counts, (True, False))
        if every_row or k > 1
    ]
    cases += [(OFFSET_SHARD, every, True, off, False) for off in OFFSETS]
    cases += [(shard, every, True, 0, True) for shard in SHARDS]
    text, images, specs = "", {}, {}
    for case in cases:
        name, src, used, spec = gather(sysnodes, *case)
        text += src
        images |= used
        specs[name] = spec
    return Module(HEAD + "".join(images.values()) + text, specs)
