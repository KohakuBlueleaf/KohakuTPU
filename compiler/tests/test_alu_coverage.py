"""Every ALU opcode the ISA carries, and the word each one lowers to.

On the GENERATED WORDS, not a device run: a wrong operand slot is a legal word
computing something else, which a numeric check on a lucky operand would pass.
Slots are read off `src/kohakutpu/vector/vec_alu.v:144-161`.

**This has a blind spot, and it cost a real bug.** A word check cannot see WHICH
REGISTER FILE an opcode writes. `VCMPLT` was lowered here as a one-word binary
with its operand in `vb` -- correct in every field -- while `vec_lanes.v:503`
writes no vector register for a compare at all. `test_tinygrad_ops.py` runs the
numbers and is what caught it. A comparison now reaches the machine only as
`Select`: into P0, then both arms as `VMOV` under `pm`.

Imports no kernel and no op: the compiler is what is under test.
"""

import pytest
from kohakutpu.hw import vector as V
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import BINARY, COMPARE, UNARY, VecEmitError, lower_op

#: Opcode -> the fields it must set, beyond `vd` and `va`.
WANT = {
    OpKind.MAX: ("VMAX", {"vb": 2}),
    OpKind.MIN: ("VMIN", {"vb": 2}),
}

#: Refused, not lowered: these write P0..P3 and no vector register.
COMPARISONS = [OpKind.CMPLT, OpKind.CMPGT, OpKind.CMPEQ]


def decode(word: int) -> int:
    """The opcode of one ALU word."""
    return (word >> V.OP_SHIFT) & 0x1F if hasattr(V, "OP_SHIFT") else word


@pytest.mark.parametrize("kind", sorted(WANT, key=lambda k: k.value))
def test_a_binary_op_is_one_word_with_its_operand_in_vb(kind):
    """One word, and the second operand in the slot the RTL compares."""
    op, extra = WANT[kind]
    got = lower_op(kind, [1, 2], dst=3)
    assert len(got) == 1, f"{kind.value} lowered to {len(got)} words"
    assert got == [V.alu(op, vd=3, va=1, **extra)]


@pytest.mark.parametrize("kind", COMPARISONS, ids=lambda k: k.name)
def test_a_comparison_is_refused_and_the_refusal_names_the_mechanism(kind):
    """A refusal has to say the way through, or it is just a dead end."""
    with pytest.raises(VecEmitError) as caught:
        lower_op(kind, [1, 2], dst=3)
    why = str(caught.value)
    assert "PREDICATE register" in why and "vec_lanes.v:503" in why
    assert "pm" in why, "the refusal must name the field that reaches it"


def test_select_puts_the_predicate_in_vc():
    """`vec_alu.v:146` reads the predicate from `s1_c`, not from `va`.

    The order `(cond, on_true, on_false)` is the DSL's, so this pins BOTH: that
    the predicate reaches `vc` and that the true value is the one in `va`.
    """
    got = lower_op(OpKind.SELECT, [7, 8, 9], dst=3)
    assert got == [V.alu("VSEL", vd=3, va=8, vb=9, vc=7)]


def test_fma_is_one_word_and_one_rounding():
    """`a*b + c` as a single opcode, which is the whole reason it exists."""
    got = lower_op(OpKind.FMA, [1, 2, 4], dst=3)
    assert got == [V.alu("VFMA", vd=3, va=1, vb=2, vc=4)]


def test_div_is_still_two_words():
    """No `VDIV` in the ALU, so a divide is the reciprocal then a multiply."""
    got = lower_op(OpKind.DIV, [1, 2], dst=3)
    assert len(got) == 2


def test_sqrt_is_still_refused_and_says_which_two_to_ask_for():
    """The ALU carries OP_INV and OP_RSQRT and no root.

    Returning the reciprocal root here is a wrong number with no fault, which is
    what this refusal exists to prevent.
    """
    with pytest.raises(VecEmitError, match="sqrt_approx"):
        lower_op(OpKind.SQRT, [1], dst=2)


def test_every_op_the_isa_carries_has_a_lowering_or_a_reason():
    """No ALU opcode is silently unreachable from the IR.

    `VCVT` and `VSHUF` have no OpKind yet and are listed so that adding one is a
    deliberate act rather than a discovery.
    """
    reachable = {UNARY[k] for k in UNARY} | {BINARY[k][0] for k in BINARY}
    reachable |= {"VINV", "VMUL", "VFMA", "VSEL", "VRED"}
    # Through `Select` only -- a compare into P0 and two `VMOV`s under `pm`.
    reachable |= {"VMOV", *COMPARE.values()}
    unreached = {
        name
        for name in V.OPS
        if name.startswith("V")
        and name not in reachable
        and name
        not in {
            "VLD",
            "VST",
            "VBCAST",
            "VSETVL",
            "VSETMODE",
            "VSETI",
            "VLOOP",
            "VBAR",
            "VFILL",
            "VDRAIN",
            "VHALT",
            "VRED",
        }
    }
    assert unreached == {"VCVT", "VSHUF", "VFNMA"}, unreached
