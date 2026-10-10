"""The streaming RUN pipeline a vector kernel runs in.

Two L1 regions by parity, each of `inputs` slots of `words` words (one slot an
input stream, at most two: a core has eight descriptors), and an optional
resident block after them, filled once. RUN k (r = k mod 2) waits for region
r's fills, drains region 1-r's slot 0 (RUN k-1's result), refills region 1-r
with RUN k+1's inputs, and computes region r, its result in slot 0; an epilogue
RUN drains the last region. The drain and the fills run in the load/store
engine beside RUN k's compute, so neither is exposed: a walk is one at a time,
so the drain has read every word before the fill's requests go out.

Before RUN k the node sets `F[s][1-r]` to RUN k+1's input `s` and `D[1-r]` to
RUN k-1's output. RUN 0 has no previous result: its drain writes region 1's
stale words to a sink (`send`).

Each body is ordered by `vsched` against the core's timing model.
"""

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.vector import Bar, Desc, Dims, Halt, Image, Run, Vdrain, Vfill

AD_L1 = 0
#: Fill descriptors by input slot, then parity; drain descriptors by parity.
F, D = ((1, 2), (5, 6)), (3, 4)
#: The resident block's fill.
R = 7
L1_WORDS = 512


def walk_offsets(dims) -> list[int]:
    """L1 words one VL=128 load/store touches through a descriptor walk."""
    offs = [0]
    for stride, bound in dims:
        offs = [o + stride * k for k in range(bound) for o in offs]
    return offs


def slot(r: int, s: int, words: int, inputs: int) -> int:
    """L1 word where region `r`'s slot `s` starts."""
    return (r * inputs + s) * words


def resident_at(words: int, inputs: int) -> int:
    """L1 word where the resident block starts."""
    return 2 * inputs * words


def l1_map(
    l1_dims, words: int, inputs: int = 1, resident: int = 0, walks=None
) -> vsched.L1Map:
    """`walks` adds a kernel's own load/store descriptors: ``{ad: dims}``, base 0."""
    fills = {a: words for a in (*F[0], *F[1][: 2 * (inputs - 1)], *D)}
    if resident:
        fills[R] = resident
    every = {AD_L1: (0, walk_offsets(l1_dims))}
    every.update({ad: (0, walk_offsets(d)) for ad, d in (walks or {}).items()})
    return vsched.L1Map(walks=every, fills=fills)


def _check(words: int, inputs: int, resident: int, scratch: int = 0) -> None:
    if not 1 <= inputs <= 2:
        raise ValueError(f"{inputs} input streams; a core's descriptors hold two")
    if resident_at(words, inputs + scratch) + resident > L1_WORDS:
        raise ValueError(
            f"{inputs} inputs and {scratch} scratch slots of {words} words a "
            f"region and {resident} resident words pass L1's {L1_WORDS}"
        )


def programs(
    head: list,
    body,
    words: int,
    l1_dims,
    inputs: int = 1,
    resident: int = 0,
    walks=None,
    scratch: int = 0,
    shared: bool = False,
) -> list:
    """``[prologue, even, odd, last even, last odd]``; `body(slots)` computes one
    region from its slots' L1 bases into ``slots[0]``: the `inputs` filled
    slots, then `scratch` slots nothing fills or drains. `walks` names the
    kernel's other L1 descriptors (``{ad: dims}``, base 0), set up by `send`'s
    `setup`.

    `shared`: one compute image for both regions, its slots relative to
    `AD_L1`'s base, which the node sets to the region before each RUN; the
    even and odd images then only wait, drain and fill (the other region) and
    a sixth image computes. Half the instruction memory, one RUN more a step.
    """
    _check(words, inputs, resident, scratch)
    n = inputs + scratch
    l1 = l1_map(l1_dims, words, inputs, resident, walks)
    pro = [Vfill(F[s][0], slot(0, s, words, n)) for s in range(inputs)]
    if resident:
        pro.append(Vfill(R, resident_at(words, n)))
    out = [tuple(pro + [Halt()])]
    for r in (0, 1):
        mine = [slot(r, s, words, n) for s in range(n)]
        other = [slot(1 - r, s, words, n) for s in range(n)]
        code = [Bar(), Vdrain(D[1 - r], other[0])]
        code += [Vfill(F[s][1 - r], other[s]) for s in range(inputs)]
        if shared:
            out.append(tuple(code + [Halt()]))
            continue
        code += body(mine)
        out.append(tuple(head + vsched.schedule(code, l1) + [Halt()]))
    out += [(Vdrain(D[r], slot(r, 0, words, n)), Halt()) for r in (0, 1)]
    if shared:
        rel = [slot(0, s, words, n) for s in range(n)]
        out.append(tuple(head + vsched.schedule(body(rel), l1) + [Halt()]))
    return out


def image_words(progs) -> int:
    """Instruction-memory words the images take."""
    return sum(len(i.words()) for p in progs for i in p)


#: Node cycles one instruction-memory word costs to install on a core that
#: does not hold the image (MEASURED, card_v9_1n: a package's node cycles not
#: resident minus resident, over the words installed -- softmax and layer norm
#: 32x128 at 64 and 128 words a RUN, 50.4 to 50.9).
INSTALL = 51


def send(prog, core, *args, **kw) -> int:
    """`ops` queued on `core`; returns the instruction words the programs take."""
    ops, size = stream_ops(*args, **kw)
    prog.send(core, *ops)
    return size


def stream_ops(
    progs,
    l1_dims,
    words: int,
    srcs,
    dst: int,
    nruns: int,
    step: int,
    resident_src: int | None = None,
    resident: int = 0,
    setup=(),
    sink: int | None = None,
    slots: int | None = None,
) -> tuple[list, int]:
    """``(ops, image words)`` for `nruns` RUNs: input `s` from ``srcs[s]``,
    output to `dst`, `step` bytes apart a RUN; the resident block's `resident`
    words from `resident_src`; `setup` (descriptor ops) before the first RUN.
    `slots`: a region's slots, inputs and scratch (`programs`), when the
    images are `shared`.

    `sink` (`words` words) takes RUN 0's stale drain. Without one it lands on
    RUN 1's output, which a later drain overwrites -- unless the output IS an
    input (in place): then it lands on RUN 1's input before RUN 0 fills it
    (MEASURED, card_v9_1n: 5% of a fused epilogue wrong), so in place needs one.
    """
    srcs = [srcs] if isinstance(srcs, int) else list(srcs)
    inputs = len(srcs)
    span = nruns * step
    if sink is None and any(s < dst + span and dst < s + span for s in srcs):
        raise ValueError("an in-place stream needs a `sink` for RUN 0's stale drain")
    first = sink if sink is not None else dst + min(1, nruns - 1) * step
    pcs, at = [], 0
    for p in progs:
        pcs.append(at)
        at += sum(len(i.words()) for i in p)
    shared = len(progs) == 6
    region = slot(1, 0, words, slots or inputs)
    walk = ((V.WORD_BYTES, words),)
    ops = [Image(p, pc) for p, pc in zip(progs, pcs, strict=True)]
    ops += [Desc(AD_L1, 0), Dims(AD_L1, l1_dims)]
    ops += [Dims(a, walk) for s in range(inputs) for a in F[s]]
    ops += [Dims(a, walk) for a in D]
    ops += [Desc(F[s][0], srcs[s]) for s in range(inputs)]
    if resident:
        ops += [Dims(R, ((V.WORD_BYTES, resident),)), Desc(R, resident_src)]
    ops += list(setup)
    ops.append(Run(pcs[0]))
    for k in range(nruns):
        r = k % 2
        nxt = (k + 1) * step if k + 1 < nruns else k * step
        prev = dst + (k - 1) * step if k else first
        ops += [Desc(F[s][1 - r], srcs[s] + nxt) for s in range(inputs)]
        ops += [Desc(D[1 - r], prev), Run(pcs[1 + r])]
        if shared:
            ops += [Desc(AD_L1, r * region), Run(pcs[5])]
    r = (nruns - 1) % 2
    ops += [Desc(D[r], dst + (nruns - 1) * step), Run(pcs[3 + r])]
    return ops, at
