"""A unit-ISA mutant the Rust stack carries reaches what a card is sent: the
first tier-1 card vector listing it, compiled through `_rs`
with and without the mutant, gives different packages, or packages the
stack's own checks refuse. A mutant applied only
where some builder never reads it (a table one constructor edits, an id one
encoder resolves) leaves the bytes equal and fails here. Skipped on a
`_rs` built without the ``mutants`` feature."""

import pytest
import runners  # noqa: F401  (registers the runners)
from harness import vectors
from harness.fresh import fresh
from runners.target import isa as R

from kohakutpu.application.runtime import packages as build
from kohakutpu.application.runtime.device import Device
from kohakutpu.language import examples
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import kernel, rs

#: One mutant per kind of unit-ISA layout point: an opcode table, field
#: orders of the config, control, DMA and walk formats, a transform id.
NAMES = [
    "isa.vector/opcodes",
    "isa.vector/cfg_fields",
    "isa.vector/ctl_fields",
    "isa.vector/dma_fields",
    "isa.vector/walk_fields",
    "isa.mover/xform",
]
CARD = ("isa.card.example", "isa.card.kernel")


def source(v) -> tuple:
    """`(module, fn, level)` the vector's runner compiles."""
    inp = v["input"]
    if v["runner"] == "isa.card.kernel":
        mx = inp.get("pack") in ("mx7a", "mx7b")
        text = inp.get("text") or R.TEMPLATE.format(
            body="\n".join("    " + ln for ln in inp["body"]),
            pack=f"    vpack v2 -> l1[16] {inp.get('pack', 'f16')}",
            yw=4 if mx else 8,
            yn=64 if mx else 128,
        )
        return module.read(text), inp.get("fn", "t"), "l1"
    name, _ = R.EXAMPLES[inp["case"]]
    if inp["case"].endswith(".l2"):
        return examples.load(examples.example(name, "l2")), name, "l2"
    m = examples.load(examples.example(name))
    return m, m.names()[0], "l1"


def sent(v) -> dict:
    """Each node's lowered package bytes for the vector, every parameter at
    its own 2 MB-aligned address."""
    dev = Device(kind="offline", board=v["machine"])
    mach = dev.machine
    src, fn, level = source(v)
    with fresh(None):
        c = kernel.compile(src, fn, mach, level=level)
        params = c.l1.body(fn, "l1").params
        binds = {p: 0x200000 * (i + 1) for i, (p, _) in enumerate(params)}
        progs = build.placed(c.programs(binds), dev.card.nodes)
        built = build.built(progs, mach, rs.fetch(mach))
    return {n: low.to_bytes() for n, (_, low, _) in built.items()}


def mutated(v) -> dict | str:
    """`sent(v)` under a mutant, or the refusal the stack's own checks
    (the L1 verifier, board admission) raise on the bytes it changed."""
    try:
        return sent(v)
    except (TextError, ValueError) as e:
        return f"refused: {e}"


@pytest.mark.parametrize("name", NAMES)
def test_a_carried_isa_mutant_changes_the_packages_rust_builds(name):
    if name not in rs.mutants():
        pytest.skip("_rs carries no mutants (built without the `mutants` feature)")
    v = next(x for x in vectors.load() if x["runner"] in CARD and name in x["mutants"])
    base = sent(v)
    with rs.mutant(name):
        got = mutated(v)
    assert got != base, f"{name} leaves {v.id}'s packages unchanged"
