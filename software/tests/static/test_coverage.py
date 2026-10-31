"""The coverage contract: what one level can write, the next level's corpus or
simulator takes, so a stage's proof chains into the stage below it."""

import pathlib
import re

SOFTWARE = pathlib.Path(__file__).resolve().parents[2]
COMPILER = SOFTWARE / "kohakutpu" / "compiler" / "crates"
SIM = COMPILER / "ktpu-sim" / "src"
ANCHORS = SOFTWARE / "tests" / "fixtures" / "anchors" / "core"
MOVER_OPS = {"copy", "quant", "gt4", "fill", "gen", "gather", "scatter"}

#: L0 steps no lowered package of the current stack carries, each by why.
L0_OUTSIDE = {
    "Repeat": "lowered to entries",
    "Ring": "a multi-node signal: machine/interlink",
    "WaitBell": "a multi-node signal: machine/interlink",
    "WaitSlot": "a multi-node signal: machine/interlink",
    "Signal": "firmware's: firmware/emu",
    "Settle": "firmware's: firmware/emu",
    "Fetch": "lowered to entries",
    "Store": "written by no compiled kernel",
    "Alu": "written by no compiled kernel",
    "Jump": "written by no compiled kernel",
    "Patch": "written by no compiled kernel",
    "Send": "a multi-node transfer: machine/interlink",
    "Recv": "a multi-node transfer: machine/interlink",
    "Ctrl": "firmware's: firmware/emu",
}


def math_ops() -> set[str]:
    text = (COMPILER / "ktpu-isa/src/unit/vector/core.rs").read_text(encoding="utf-8")
    body = re.search(r"pub enum MathOp \{(.*?)\}", text, re.DOTALL)[1]
    return {name.lower() for name in re.findall(r"(V\w+),", body)}


def test_every_math_op_has_a_hardware_anchor():
    written = set()
    for path in ANCHORS.glob("*.ktpu"):
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts:
                written.add(parts[0])
    assert math_ops() <= written, math_ops() - written


def test_every_math_op_has_an_l2_form():
    text = (SIM / "l2" / "math.rs").read_text(encoding="utf-8")
    named = set(re.findall(r"MathOp::(V\w+)", text))
    assert {op.capitalize() for op in math_ops() - {"vexp2d"}} <= {n for n in named} | {
        "Vcmplt",
        "Vcmpgt",
        "Vcmpeq",
    }


def dsl_ops() -> set[str]:
    text = (SOFTWARE / "kohakutpu/language/kohakutpu/language/dsl/ops.py").read_text(
        "utf-8"
    )
    groups = re.findall(
        r"^(?:UNARY|BINARY|TERNARY|COMPARE|REDUCE) = \((.*?)\)", text, re.MULTILINE
    )
    return {op for group in groups for op in re.findall(r'"(\w+)"', group)} | MOVER_OPS


def fixture_ops() -> set[str]:
    corpus = (
        SOFTWARE / "tests/fixtures/l2",
        SOFTWARE / "kohakutpu/language/kohakutpu/language/examples/l2",
    )
    written = set()
    for root in corpus:
        for path in root.rglob("*.ktpu"):
            text = path.read_text(encoding="utf-8")
            written |= set(re.findall(r"= (\w+)\b", text))
            written |= set(re.findall(r"^\s*(\w+) \w+\[?", text, re.MULTILINE))
    return written


def test_every_l2_op_is_in_the_stage_corpus():
    assert dsl_ops() <= fixture_ops(), dsl_ops() - fixture_ops()


def test_every_l0_step_runs_or_says_where_it_is_proved():
    text = (
        COMPILER.parent.parent.parent
        / "kohakuaccel/compiler/crates/ka-package/src/format/mod.rs"
    )
    source = text.read_text(encoding="utf-8")
    body = re.search(r"pub enum Op \{(.*?)\n\}", source, re.DOTALL)[1]
    steps = set(re.findall(r"^\s*(\w+)\s*=", body, re.MULTILINE))
    run = set(
        re.findall(r"Op::(\w+)", (SIM / "l0" / "mod.rs").read_text(encoding="utf-8"))
    )
    assert steps - run - {"End"} <= set(L0_OUTSIDE), steps - run - set(L0_OUTSIDE)
