"""P15: dispatch cost by stream size, on 1, 2 or 4 sysnodes feeding their own units at
once (after a start barrier, its token ledger carried). Each stream does N = 1, 8,
32, 128 or 512 pieces of real work, every piece leaving its own tag words in y
(x is host tag words, written once; y == x per feeder, every word), so a dropped
send shows as POISON, never as a fast run. Feeder slices start on 64-byte
boundaries; alignment gaps remain POISON. Kinds:

- `sends`: N `send vc[0]` of one `run one(..)` (two DESC words and a RUN a piece,
  the image resident after the first), one wait at the end;
- `waits`: the same with a wait after every send (N waits);
- `group`: N runs in one send block (one send group);
- `repeat`: one `send vc[0] repeats=N` template advancing both descriptors;
- `moves`: N blocking mover steps of one 64-byte line (DE entries, no CU words).

Image words: 8 runs of a one-word copy carrying 0, 32 or 128 extra `seti` (2 IMEM
words each), the resident image's length the only difference. A package with no
work on each feeder count is the overhead floor. Cold install against retained
replay is the runner's reps (rep 0 loads images, later reps find them resident).

Not here: separate fetch / validation / MMIO / room / tail stamps (the fixed image
gives each sysnode's cycles and phases: a composite). BLOCKED(KAOS run- and
bind-validation): no KAOS execution adapter. BLOCKED(shared-DRAM descriptor
fetch): a `fetch` step is a `model` finding the static verifier cannot decide."""

import itertools

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags, poison

COUNTS = (1, 8, 32, 128, 512)
KINDS = ("sends", "waits", "group", "repeat", "moves")
#: Extra `seti`s of the image-length cases, and their runs.
PADS = (0, 32, 128)
PAD_RUNS = 8
#: A piece's words (a core copies one, the mover one 64-byte line), a word's bytes.
PIECE = {"moves": 2}
WORD = 32
START = 1

IMAGES = """
image one(x, y)
    desc a0 = x walk=(32, 1)
    desc a1 = y walk=(32, 1)
    vfill a0 -> l1[0]
    vsync drain on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt
"""


def pad_image(pad: int) -> str:
    sets = "".join(f"    seti s1 = {k + 1}\n" for k in range(pad))
    return (
        f"\nimage pad{pad}(x, y)\n    desc a0 = x walk=(32, 1)\n"
        "    desc a1 = y walk=(32, 1)\n"
        f"{sets}    vfill a0 -> l1[0]\n    vsync drain on=fill\n"
        "    vdrain a1 <- l1[0]\n    vsync fe on=drain\n    halt\n"
    )


def stream(b: Body, n: int, kind: str, count: int, at: int, image: str) -> None:
    """Sysnode `n`'s `count` pieces into y + `at`."""
    if kind == "moves":
        walk = "(2, 32)"
        for i in range(count):
            o = 2 * WORD * i
            b.add(
                n,
                "move local=1",
                f"    copy w(x + {o}, {walk}) -> w(y + {at + o}, {walk})",
            )
        return
    if kind == "repeat":
        b.add(
            n,
            f"send vc[0] repeats={count} advances=((0, {WORD}), (1, {WORD}))",
            f"    run {image}(x, y + {at})",
            "wait vc[0]",
        )
        return
    runs = [
        f"    run {image}(x + {WORD * i}, y + {at + WORD * i})" for i in range(count)
    ]
    if kind == "group":
        b.add(n, "send vc[0]", *runs, "wait vc[0]")
        return
    for r in runs:
        b.add(n, "send vc[0]", r)
        if kind == "waits":
            b.add(n, "wait vc[0]")
    b.add(n, "wait vc[0]")


def feeders(sysnodes, kind: str, count: int, image: str, name: str):
    b = Body(ledger=len(sysnodes) > 1)
    if len(sysnodes) > 1:
        b.barrier(list(sysnodes), START)
    words = count * PIECE.get(kind, 1)
    stride = -(-words * WORD // 64) * 64
    for q, n in enumerate(sysnodes):
        if count:
            stream(b, n, kind, count, q * stride, image)
    params = "unused: u64[4] @dram"
    if count:
        half = words * WORD // 2
        size = (len(sysnodes) - 1) * stride // 2 + half
        params = f"x: f16[{half}] @dram, y: f16[{size}] @dram"
    if not b.lines:
        blocks = "".join(f"\n    node {n}" for n in sysnodes)
        return f"\nfn {name}.l1({params}){blocks}\n", b, words
    return b.fn(name, params), b, words


def spec(b: Body, sysnodes, words: int) -> Spec:
    outs = ("slots",) if b.ledger else ()
    if not words:
        return Spec(outs, ledger=b.ledger_of() if b.ledger else None)

    def want(ins: dict) -> dict:
        half = words * WORD // 2
        stride = -(-words * WORD // 64) * 32
        y = poison(np.float16, (len(sysnodes) - 1) * stride + half)
        for q in range(len(sysnodes)):
            y[q * stride : q * stride + half] = ins["x"]
        return {"y": y}

    return Spec(
        ("y", *outs),
        want,
        inputs=host_tags({"x": (words * WORD, np.float16, 0)}),
        fresh=False,
        ledger=b.ledger_of() if b.ledger else None,
    )


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    groups = [tuple(sysnodes[:k]) for k in (1, 2, 4) if k <= len(sysnodes)]
    text, specs = HEAD + IMAGES + "".join(map(pad_image, PADS)), {}

    def add(group, kind: str, count: int, image: str, name: str) -> None:
        nonlocal text
        src, b, words = feeders(group, kind, count, image, name)
        text += src
        specs[name] = spec(b, group, words if count else 0)

    for group in groups:
        tag = "".join(map(str, group))
        add(group, "sends", 0, "one", f"p15_{tag}_empty")
        for kind, count in itertools.product(KINDS, COUNTS):
            add(group, kind, count, "one", f"p15_{tag}_{kind}_n{count}")
    for pad in PADS:
        add(groups[0], "sends", PAD_RUNS, f"pad{pad}", f"p15_image_pad{pad}")
    return Module(text, specs)
