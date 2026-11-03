"""C4: the release-frontier witness, executed. `witness/release_frontier.ktpu`
(sysnodes 0 and 1) compiled by the current compiler for the board, as emitted, with
sysnode 1 copying words 0, 127, 128 and 255 of a and of b to `seen` right after
its wait slot (before it reads them): `c4_frontier`; and the same with sysnode 0's
vc[0] held behind a spin of SPIN fills, so vc[1] finishes first:
`c4_frontier_late`. A ring that does not wait for vc[0] releases sysnode 1 to a
still POISON a, and `seen` shows it. y (f32 add, f16 pack) is checked numerically.

`h/frontier.py` prints the waits emitted before each ring and the units they leave
uncovered; run it on the same file and board before this row. The emitted ordering
is checked whether or not the late run manifests: no manifestation is not proof.
Not here: h_ir's proposed compiler correction (not applied); the later-A-consumer
after B-only release and read-before-overwrite members of the witness family."""

import pathlib

import numpy as np

from h.frontier import emitted
from h.gen import Module, Spec
from h.gen.body import SEEN_REG, host_tags

WITNESS = pathlib.Path(__file__).parent / "witness" / "release_frontier.ktpu"
#: The spin's fills of 32 words ahead of vc[0]'s copy.
SPIN = 2048
#: The u64 words of a and b sysnode 1 copies at acquisition.
OBSERVED = (0, 127, 128, 255)
BYTES = 8 * 128 * 2

IMAGES = """
image spin(n, l)
    desc a0 = l walk=(32, 32)
    seti s0 = n
    loop s0
        vfill a0 -> l1[0] rel
        vsync fe on=fill
    halt
"""


def edited(body: list[str], late: bool) -> list[str]:
    """The emitted fn body with sysnode 1's observation (and the spin)."""
    out, sysnode, spun, seen = [], -1, not late, False
    for line in body:
        s = line.strip()
        if line.startswith("    node "):
            sysnode = int(s.split()[1])
        if sysnode == 0 and s == "send vc[0]" and not spun:
            out += ["        send vc[0]", f"            run spin({SPIN}, l)"]
            spun = True
        out.append(line)
        if sysnode == 1 and s.startswith("wait slot") and not seen:
            for k, (buf, w) in enumerate((b, w) for b in "ab" for w in OBSERVED):
                out += [
                    f"        {SEEN_REG} = load {buf} + {8 * w}",
                    f"        store seen + {8 * k} <- {SEEN_REG}",
                ]
            seen = True
    return out


def want(ins: dict) -> dict:
    a, b = ins["x0"].ravel(), ins["x1"].ravel()
    y = (a.astype(np.float32) + b.astype(np.float32)).astype(np.float16)
    seen = np.concatenate([v.view(np.uint64)[list(OBSERVED)] for v in (a, b)])
    return {"a": a, "b": b, "y": y, "seen": seen}


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    l1_text, name = emitted(WITNESS, board, max(sysnodes) + 1)
    head, fn = l1_text.split(f"\nfn {name}.l1(")
    params, body = fn.split(")\n", 1)
    text, specs = head + IMAGES, {}
    tagged = host_tags({"x0": (BYTES, np.float16, 0), "x1": (BYTES, np.float16, 1)})
    for late in (False, True):
        fn_name = "c4_frontier_late" if late else "c4_frontier"
        extra = ", seen: u64[8] @dram" + (", l: f16[512] @dram" if late else "")
        lines = edited(body.rstrip("\n").split("\n"), late)
        text += f"\nfn {fn_name}.l1({params}{extra})\n" + "\n".join(lines) + "\n"
        specs[fn_name] = Spec(
            ("a", "b", "y", "seen"), want, inputs=tagged, numeric=("y",)
        )
    return Module(text, specs)
