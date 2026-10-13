"""Bank-aware placement (l2/banks.py): on a two-copy register file the
values an op reads in one beat are placed in opposite banks, and the lint
names every beat that still holds the math engine or starves the pack."""

from dataclasses import replace

from kohakutpu.compiler import target as T
from kohakutpu.compiler import verify
from kohakutpu.compiler.l2 import banks, pipeline
from kohakutpu.language import examples
from kohakutpu.language.text import module

LEAKY = [
    ("vunpk {0} <- l1[0] f16", ("%1",)),
    ("vmul {0}, {1}, s0", ("%2", "%1")),
    ("vmax {0}, {1}, {2}", ("%3", "%1", "%2")),
    ("vpack {0} -> l1[64] f16", ("%3",)),
]


def image(code: list, regs: dict) -> str:
    lines = [t.format(*(regs[s] for s in syms)) for t, syms in code]
    return "image k()\n" + "".join(f"    {ln}\n" for ln in [*lines, "halt"])


def test_two_reads_of_one_beat_go_to_opposite_banks():
    regs = {"%1": "v0", "%2": "v1", "%3": "v0"}
    before = banks.lint(module.read(image(LEAKY, regs)), 2)
    assert len(before) == 1 and "8 leave the pack engine no copy" in before[0].text
    placed = banks.rotate(LEAKY, 2)
    assert banks.lint(module.read(image(placed, regs)), 2) == []
    # x and 0.1 x one chunk apart: one of the two is rotated
    rho = banks.choose(LEAKY, 2)
    assert len({"%1", "%2"} & set(rho)) == 1
    # a rotated walk names its chunk count
    assert all(
        " n=8" in t for t, syms in placed if t.startswith("vunpk") and "[1:]" in t
    )


def test_four_copies_need_no_placement():
    assert banks.rotate(LEAKY, 4) is LEAKY
    regs = {"%1": "v0", "%2": "v1", "%3": "v0"}
    assert banks.lint(module.read(image(LEAKY, regs)), 4) == []


def test_three_words_of_one_bank_hold_the_math_engine():
    text = "image k()\n    vfma v0, v1, v2, v3\n    halt\n"
    (f,) = banks.lint(module.read(text), 2)
    assert f.kind == "bank" and "8 beats hold the math engine" in f.text


def test_verify_lint_checks_every_image_for_the_machines_copies():
    m = examples.load(examples.example("silu"))  # hand-written: x * sigmoid(x) in place
    assert verify.lint(m, T.machine()) == []  # four copies: no rule to break
    found = verify.lint(m, T.machine(core="v21"))
    assert found and all(f.kind == "bank" and "pack engine" in f.text for f in found)


def test_compiled_vector_examples_hold_no_math_beat_on_two_copies():
    for name in ("softmax", "leaky", "layernorm", "silu", "add"):
        m = examples.load(examples.example(name, "l2"))
        mach = T.machine(nodes=1, vregs=128, vcopies=2)
        l1 = pipeline.compile_l1(replace(m, vregs=128), name, mach)
        held = [f for f in banks.lint(l1, 2) if "hold the math engine" in f.text]
        assert held == [], (name, [f.text for f in held[:3]])
