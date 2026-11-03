"""P14: whole-CPU-L1 maintenance, as far as L1 reaches it today. Each of 1, 2 or 4
sysnodes (after a start barrier) writes DCACHE flush, invalidate or both, ROUNDS
times, and polls it from L1: up to RISE polls for busy to rise, then until it
falls. Each round stores (busy seen while rising, rise polls, fall polls) to
`polls`: busy rise then fall, not an immediate idle read. The CTRL step itself
(`ka_dcache`) polls busy at once, before the sweep may have started, so the L1
polls after it are the measurement; the counts are observed, not compared.

BLOCKED(cached CPU store): the package STORE and LOAD steps go through the uncached
alias (`ka_st64` / `ka_ld64` set KA_UNCACHED), so no L1 step makes a dirty or
stale cached line: the 1/16/64 dirty-line sweeps, unrelated private dirties,
shared clean lines, CPU -> CU -> CPU ownership handoff, forced evictions and the
broken-ownership witnesses need a cached CPU access the fixed images do not
expose. These sweeps are of the lines the firmware itself holds."""

import itertools

from h.gen import Module, Spec
from h.gen.body import HEAD, Body

#: DCACHE writes: [0] flush, [1] invalidate. An invalidate alone is never issued: it
#: drops the dispatching firmware's own dirty lines (on v9t6 it left sysnode 1's
#: ring-credit state behind, and every later ring of that boot timed out).
OPS = {"flush": 1, "both": 3}
ROUNDS = 4
#: Polls for busy to rise at most; a rise count past it never saw busy.
RISE = 64
START = 1
#: (busy seen, rise polls, fall polls) a round.
FIELDS = 3


def sweep(b: Body, n: int, op: int, i: int, at: int) -> None:
    """One DCACHE write and its polls, its fields stored at byte `at`."""
    b.add(
        n,
        f"ctrl dcache <- {op}",
        "r10 = set 0",
        f"r6 = set {RISE}",
        f"label rise{i}",
        "r11 = ctrl dcache",
        "r10 = add r10, 1",
        f"r12 = mul r11, {RISE}",
        "r12 = add r12, r10",
        f"goto rise{i} if r12 < r6",
        "r9 = add r11, 0",
        "r13 = set 0",
        "r7 = set 0",
        f"label fall{i}",
        "r11 = ctrl dcache",
        "r13 = add r13, 1",
        f"goto fall{i} if r7 < r11",
        f"store polls + {at} <- r9",
        f"store polls + {at + 8} <- r10",
        f"store polls + {at + 16} <- r13",
    )


def cpus(sysnodes: list[int], name: str, op: int):
    b = Body()
    if len(sysnodes) > 1:
        b.barrier(sysnodes, START)
    for k, n in enumerate(sysnodes):
        for i in range(ROUNDS):
            sweep(b, n, op, i, 8 * FIELDS * (k * ROUNDS + i))
    tag = "".join(map(str, sysnodes))
    fn = f"p14_{tag}_{name}"
    words = FIELDS * ROUNDS * len(sysnodes)
    return fn, b.fn(fn, f"polls: u64[{words}] @dram")


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD, {}
    groups = [[n] for n in sysnodes]
    groups += [list(sysnodes[:k]) for k in (2, 4) if k <= len(sysnodes)]
    for group, (name, op) in itertools.product(groups, OPS.items()):
        fn, src = cpus(group, name, op)
        text += src
        specs[fn] = Spec(("polls",))
    return Module(text, specs)
