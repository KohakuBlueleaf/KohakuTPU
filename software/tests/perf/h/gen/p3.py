"""P3: one phase boundary across every sysnode, three ways, fenced and plain, K
phases a package after a start barrier: all-to-all (each rings every other, then
waits for all of them: n(n-1) directed rings a phase), a leader's gather and
broadcast (each leader), and a chain down the line and back. Cycles a phase are the
slope over K; each sysnode stores its phase count."""

import itertools

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, counts

#: Phases a package (even: all-to-all runs two a trip).
PHASES = (2, 16)
START, PHASE, BACK = 1, 2, 3


def alltoall(b: Body, every: list[int], plain: bool, k: int) -> None:
    """Two phases a trip on alternate slots: a sysnode runs at most one phase ahead,
    so a ring of the next phase never counts toward this one."""
    for slot in (PHASE, BACK):
        for n in every:
            for m in every:
                if m != n:
                    b.ring(n, m, slot, plain=plain, times=k // 2)
            b.take(n, slot, len(every) - 1)


def gather(b: Body, every: list[int], lead: int, plain: bool, k: int) -> None:
    rest = [n for n in every if n != lead]
    for n in rest:
        b.ring(n, lead, PHASE, plain=plain, times=k)
    b.take(lead, PHASE, len(rest))
    for n in rest:
        b.ring(lead, n, BACK, plain=plain, times=k)
        b.take(n, BACK, 1)


def chain(b: Body, every: list[int], plain: bool, k: int) -> None:
    """Down the line, each waiting for the one before; then back up."""
    for a, c in itertools.pairwise(every):
        b.ring(a, c, PHASE, plain=plain, times=k)
        b.take(c, PHASE, 1)
    for a, c in itertools.pairwise(every[::-1]):
        b.ring(a, c, BACK, plain=plain, times=k)
        b.take(c, BACK, 1)


def phase(kind: str, every: list[int], lead: int, plain: bool, k: int):
    b = Body()
    b.barrier(every, START)
    for n in every:
        b.add(n, "r1 = set 0", f"r2 = set {k}", "label trip")
    if kind == "alltoall":
        alltoall(b, every, plain, k)
    if kind == "gather":
        gather(b, every, lead, plain, k)
    if kind == "chain":
        chain(b, every, plain, k)
    step = 2 if kind == "alltoall" else 1
    for i, n in enumerate(every):
        b.add(
            n,
            f"r1 = add r1, {step}",
            "goto trip if r1 < r2",
            f"store count + {8 * i} <- r1",
        )
    fence = "plain" if plain else "fenced"
    tag = f"gather{lead}" if kind == "gather" else kind
    name = f"p3_{tag}_{fence}_k{k}"
    return name, b.fn(name, f"count: u64[{len(every)}] @dram")


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    every, size = sorted(sysnodes), len(sysnodes)
    text, specs = HEAD, {}
    kinds = [("alltoall", every[0]), ("chain", every[0])]
    kinds += [("gather", lead) for lead in every]
    for (kind, lead), plain, k in itertools.product(kinds, (False, True), PHASES):
        name, src = phase(kind, every, lead, plain, k)
        text += src
        specs[name] = Spec(("count",), lambda _, r=k, n=size: counts(r, n))
    return Module(text, specs)
