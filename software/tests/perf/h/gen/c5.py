"""C5: bounded failure cases that need no reset, each with its expected outcome.
Every sysnode first copies x to its own y (independent work: progress on every
reachable queue must complete), then:

- `c5_missing_producer`: sysnode 1 waits GO from sysnode 0, whose package is
  compiled but never issued (`Spec.issued`: a fn waiting a slot no block rings is
  refused at compile as a deadlock, L1.VER.001); sysnode 1's firmware wait bound
  expires (TIMEOUT, the expected result), its dependent copy to z never issues (z
  stays POISON), and sysnode 0's y0 stays POISON.
- `c5_stalled_consumer`: sysnode 0 rings sysnode 1's DATA slot and waits FREE
  back before its dependent copy to z; sysnode 1 first waits GO from sysnode 2
  (never issued), so it stalls before taking DATA and never rings FREE: both time
  out (expected), z stays POISON, DATA's token is left in sysnode 1's slot.
- `c5_refuse_amount` (a ring amount past the 16-bit field, L0.STEP.033) and
  `c5_refuse_line` (two mover commands writing parts of one 64-byte line,
  L1.VER.017): refused at admission, before issue; the runner never issues
  them (`runner.admission`).

The timeout fns are `Spec.expected` (verdict EXPECTED when exactly the named
sysnode times out and every other output, redzone and fault checks). A timeout
leaves ring state suspect until every die is retuned, and hrun stops a row at its
first TIMEOUT (exit 3): run each timeout fn alone, one hrun call each with
`--only '^NAME$' --faults` (a fault is a row, not an exception), then retune every
die. The refusal fns issue nothing and may run together. The progress deadline
is the firmware's wait bound (the card's timeout, recorded in meta), set before
injection; the fixed firmware gives an eventual timeout, not distributed
cancellation. BLOCKED(full CQ): no fixed-image step fills a completion queue
without host participation. BLOCKED(quarantine / post-failure epoch): the next
epoch after a timeout is the retuned row's, so stale-token consumption after a
recoverable failure is not exercised inside one session."""

import numpy as np

from h.gen import Module, Spec
from h.gen.body import HEAD, Body, host_tags, poison

#: The copies' words.
WORDS, WORD = 64, 32
#: Slots: the absent sysnode's go, the stalled consumer's data and its credit.
GO, DATA, FREE = 9, 2, 3

IMAGES = f"""
image cp(x, y)
    desc a0 = x walk=(32, {WORDS})
    desc a1 = y walk=(32, {WORDS})
    vfill a0 -> l1[0]
    vsync drain on=fill
    vdrain a1 <- l1[0]
    vsync fe on=drain
    halt
"""
ELEMS = WORDS * WORD // 2


def copy(b: Body, n: int, dst: str) -> None:
    b.add(n, "send vc[0]", f"    run cp(x, {dst})", "wait vc[0]")


def failing(sysnodes, kind: str):
    """A timeout case: its text and Spec. The fn is complete (the static checker
    proves it deadlock-free); the producer's sysnode is compiled, never issued."""
    b = Body()
    for n in sysnodes:
        copy(b, n, f"y{n}")
    first, second, third = sysnodes[:3]
    if kind == "missing_producer":
        absent, stuck = first, {second: "TIMEOUT"}
        b.ring(first, second, GO)
        b.take(second, GO, 1)
        copy(b, second, "z")
    else:
        absent, stuck = third, {first: "TIMEOUT", second: "TIMEOUT"}
        b.ring(third, second, GO)
        b.ring(first, second, DATA)
        b.take(second, GO, 1)
        b.take(second, DATA, 1)
        b.ring(second, first, FREE)
        b.take(first, FREE, 1)
        copy(b, first, "z")
    name = f"c5_{kind}"
    params = [f"x: f16[{ELEMS}] @dram", f"z: f16[{ELEMS}] @dram"]
    params += [f"y{n}: f16[{ELEMS}] @dram" for n in sysnodes]

    def want(ins: dict) -> dict:
        out = {f"y{n}": ins["x"] for n in sysnodes}
        return out | {"z": poison(np.float16, ELEMS)}

    spec = Spec(
        ("z", *(f"y{n}" for n in sysnodes)),
        want,
        inputs=host_tags({"x": (WORDS * WORD, np.float16, 0)}),
        owners={f"y{n}": n for n in sysnodes},
        issued=tuple(n for n in sysnodes if n != absent),
        expected=stuck,
    )
    return b.fn(name, ", ".join(params)), name, spec


def refused(sysnodes, kind: str):
    """An admission case: its text and Spec."""
    b = Body()
    first, second = sysnodes[0], sysnodes[1]
    if kind == "amount":
        b.add(first, f"ring mesh={second} slot={DATA} amount={1 << 16}")
        code = "L0.STEP.033"
    else:
        b.add(
            first,
            "move",
            "    copy w(x, (1, 32)) -> w(y, (1, 32))",
            "    copy w(x + 32, (1, 32)) -> w(y + 32, (1, 32))",
        )
        code = "L1.VER.017"
    name = f"c5_refuse_{kind}"
    params = f"x: f16[{ELEMS}] @dram, y: f16[{ELEMS}] @dram"
    return b.fn(name, params), name, Spec(("y",), refused=code)


def generate(sysnodes: tuple[int, ...], board: str) -> Module:
    text, specs = HEAD + IMAGES, {}
    cases = [failing(sysnodes, k) for k in ("missing_producer", "stalled_consumer")]
    cases += [refused(sysnodes, k) for k in ("amount", "line")]
    for src, name, spec in cases:
        text += src
        specs[name] = spec
    return Module(text, specs)
