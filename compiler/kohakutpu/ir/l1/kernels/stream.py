"""The streaming RUN pipeline a vector kernel runs in.

Two L1 regions of `words` words and two program variants by parity. RUN k
(r = k mod 2) waits for region r's fill, drains region 1-r (RUN k-1's result),
refills region 1-r with RUN k+1's input, and computes region r in place; an
epilogue RUN drains the last region. The drain and the fill run in the
load/store engine beside RUN k's compute, so neither is exposed: a walk is one
at a time, so the drain has read every word before the fill's requests go out.

Before RUN k the node sets `F[1-r]` to RUN k+1's input and `D[1-r]` to RUN
k-1's output. RUN 0 has no previous result: its drain writes region 1's stale
words where a later drain of the same core overwrites them (RUN 1's output, or
RUN 0's own when it is the only one).

Each body is ordered by `vsched` against the core's timing model.
"""

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1 import vsched
from kohakutpu.ir.l1.vector import Bar, Desc, Dims, Halt, Image, Run, Vdrain, Vfill

AD_L1 = 0
F, D = (1, 2), (3, 4)


def walk_offsets(dims) -> list[int]:
    """L1 words one VL=128 load/store touches through a descriptor walk."""
    offs = [0]
    for stride, bound in dims:
        offs = [o + stride * k for k in range(bound) for o in offs]
    return offs


def l1_map(l1_dims, words: int) -> vsched.L1Map:
    return vsched.L1Map(
        walks={AD_L1: (0, walk_offsets(l1_dims))}, fills={a: words for a in (*F, *D)}
    )


def programs(head: list, body, words: int, l1_dims) -> list:
    """``[prologue, even, odd, last even, last odd]``; `body(region)` computes
    one region in place."""
    l1 = l1_map(l1_dims, words)
    out = [(Vfill(F[0], 0), Halt())]
    for r in (0, 1):
        region, other = r * words, (1 - r) * words
        code = [Bar(), Vdrain(D[1 - r], other), Vfill(F[1 - r], other)] + body(region)
        out.append(tuple(head + vsched.schedule(code, l1) + [Halt()]))
    out += [(Vdrain(D[r], r * words), Halt()) for r in (0, 1)]
    return out


def send(
    prog, core, progs, l1_dims, words: int, src: int, dst: int, nruns: int, step: int
) -> int:
    """Queue `nruns` RUNs on `core` over `src`/`dst`, `step` bytes apart. Returns
    the instruction words the programs take."""
    pcs, at = [], 0
    for p in progs:
        pcs.append(at)
        at += sum(len(i.words()) for i in p)
    walk = ((V.WORD_BYTES, words),)
    ops = [Image(p, pc) for p, pc in zip(progs, pcs, strict=True)]
    ops += [Desc(AD_L1, 0), Dims(AD_L1, l1_dims)]
    ops += [Dims(a, walk) for a in (*F, *D)]
    ops += [Desc(F[0], src), Run(pcs[0])]
    for k in range(nruns):
        r = k % 2
        nxt = src + (k + 1) * step if k + 1 < nruns else src + k * step
        prev = dst + (k - 1 if k else min(1, nruns - 1)) * step
        ops += [Desc(F[1 - r], nxt), Desc(D[1 - r], prev), Run(pcs[1 + r])]
    r = (nruns - 1) % 2
    ops += [Desc(D[r], dst + (nruns - 1) * step), Run(pcs[3 + r])]
    prog.send(core, *ops)
    return at
