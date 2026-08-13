"""The generic vector emitter must reproduce the kernels that have run.

`MapKernel` is the authority: its bodies are what the card has executed. Where
the generic emitter covers the same op it must produce the SAME instruction
words, because an operand in the wrong slot is a legal word that computes
something else and nothing on the machine reports it.

Run as ``python -m tests.test_vecemit`` from ``compiler/``.
"""

from kohakutpu.hw import veckernels as VK
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import ElementwiseKernel, VecEmitError

#: name -> (ops, nin), for every BODIES entry this emitter claims to cover.
SAME = {
    "mul": ([(OpKind.MUL, [0, 1])], 2),
    "add": ([(OpKind.ADD, [0, 1])], 2),
    "div": ([(OpKind.DIV, [0, 1])], 2),
}


def test_it_matches_mapkernel_word_for_word() -> None:
    for name, (ops, nin) in SAME.items():
        mine = ElementwiseKernel(ops, nin)
        theirs = VK.MapKernel(name)
        assert mine.image == theirs.image, (
            f"{name}: image differs\n  mine   {[hex(w) for w in mine.image]}\n"
            f"  theirs {[hex(w) for w in theirs.image]}"
        )


def test_the_descriptors_match_too() -> None:
    for name, (ops, nin) in SAME.items():
        mine = ElementwiseKernel(ops, nin)
        theirs = VK.MapKernel(name)
        assert mine.static_descs() == theirs.static_descs(), name
        assert mine.batch_flits([0x100, 0x200][:nin], 0x300, 2) == theirs.batch_flits(
            [0x100, 0x200][:nin], 0x300, 2
        ), name


def test_a_chain_longer_than_one_op_still_builds() -> None:
    """`(x * y) + z`: two ops, three inputs, one image."""
    k = ElementwiseKernel([(OpKind.MUL, [0, 1]), (OpKind.ADD, [7, 2])], nin=3)
    assert k.ad_drain <= 7
    assert len(k.image) > len(ElementwiseKernel([(OpKind.MUL, [0, 1])], 2).image)


def test_max_lowers_to_one_word_now_that_the_slot_IS_known() -> None:
    """`vec_alu.v:144` is `va = cmp_lt ? s1_b : s1_a`, so the operand is `vb`.

    It used to be refused, and composed as `(|a-b| + a + b) / 2` -- four words
    for what the ALU does in one. The refusal was right while the slot was a
    guess and wrong once the RTL had been read.
    """
    k = ElementwiseKernel([(OpKind.MAX, [0, 1])], 2)
    assert k.image


def test_an_op_with_no_known_slot_is_still_refused() -> None:
    """Guessing an operand slot is how a legal word computes the wrong thing."""
    try:
        ElementwiseKernel([(OpKind.CAST, [0])], 1)
    except VecEmitError:
        return
    raise AssertionError("cast was lowered without a demonstrated operand slot")


def test_sqrt_is_REFUSED_rather_than_lowered_to_the_reciprocal_root() -> None:
    """The one that returned a wrong number with no fault, for as long as it sat here.

    `UNARY` mapped `OpKind.SQRT` to `VRSQRT`, on the line beside `RSQRT`. There
    is still no single word to correct that to, so the refusal stays even now
    that `L.sqrt_approx` exists: a root is COMPOSED, and this pins that nobody
    re-adds the row on the way to composing it.
    """
    from kohakutpu.isa.vecemit import UNARY, lower_op

    assert OpKind.SQRT not in UNARY
    assert UNARY[OpKind.RSQRT] == "VRSQRT"
    try:
        lower_op(OpKind.SQRT, [0], dst=7)
    except VecEmitError as exc:
        assert "NO square root" in str(exc)
        assert "sqrt_approx" in str(exc), "the refusal must name the way through"
        return
    raise AssertionError("sqrt was lowered, and the only word available is wrong")


def test_both_sqrt_paths_lower_to_words_this_emitter_already_had() -> None:
    """Two words and six, out of ops with demonstrated slots and no new opcode.

    The point of composing rather than lowering: neither path needs a row in
    `UNARY`, an entry in `DEMONSTRATED`, or an instruction the ISA lacks.
    """
    from kohakutpu.isa.vecemit import lower_op
    from kohakutpu.lang.vector import (
        Ref,
        Value,
        chains_of,
        leaves_of,
        sqrt_approx,
        sqrt_newton,
    )

    for build, words in ((sqrt_approx, 2), (sqrt_newton, 6)):
        expr = build(Value(Ref("x"))).expr
        lifted, final = chains_of(expr, leaves_of(expr))
        got = [op for chain in (*lifted, final) for op in chain]
        assert len(got) == words, f"{build.__name__} is {len(got)} words, not {words}"
        for kind, srcs in got:
            assert lower_op(kind, [0] * len(srcs), dst=7), kind


def test_too_many_operands_is_refused_before_the_card_sees_it() -> None:
    try:
        ElementwiseKernel([(OpKind.ADD, [0, 1])], nin=4)
    except VecEmitError as exc:
        assert "descriptors" in str(exc) or "L1" in str(exc)
        return
    raise AssertionError("a kernel needing more than 8 descriptors was built")


def test_the_reduction_idiom_matches_the_flash_kernel() -> None:
    """`flash_rows_kernel` has run; its VRED sequence is the authority.

    A chained instruction in TREE that is not a VRED faults with F_OPCODE, so
    the mode switch either side is part of the idiom rather than tidiness.
    """
    from kohakutpu.hw import vector as V
    from kohakutpu.isa.vecemit import reduce_row

    mine = reduce_row(OpKind.RMAX, src=0, dst=1, sreg=1)
    theirs = [
        V.vsetmode(V.TREE),
        V.vred(1, 0, "MAX"),
        V.vsetmode(V.FLAT),
        V.vbcast(1, 1),
    ]
    assert mine == theirs, f"\n  mine   {mine}\n  theirs {theirs}"


def test_a_row_wider_than_one_pass_is_refused() -> None:
    from kohakutpu.isa.vecemit import RowReduceKernel

    RowReduceKernel(OpKind.SUM, rows=4, cols=128)
    for bad in (129, 100):
        try:
            RowReduceKernel(OpKind.SUM, rows=4, cols=bad)
        except VecEmitError:
            continue
        raise AssertionError(f"a {bad}-wide row was accepted")


def test_a_reduction_kernel_ends_in_drain_and_halt() -> None:
    from kohakutpu.hw import vector as V
    from kohakutpu.isa.vecemit import RowReduceKernel

    k = RowReduceKernel(OpKind.SUM, rows=8, cols=64)
    assert k.image[-1] == V.vhalt()
    assert k.image[-2] == V.vdrain(k.AD_DRAIN, k.rw)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"  {len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
