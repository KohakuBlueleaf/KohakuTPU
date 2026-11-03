"""P6's host models against independent references: the accumulator's FP22 and
drain vectors (ktpu-sim NUM.ACC tests), the witness data's exact integer
product, the seeded data's quantised-operand product, and the order of the
protocol the generator emits."""

import numpy as np

from h.gen import p6
from kohakutpu.language.numerics import mxfp7

SYSNODES = (0, 1, 2, 3)


def test_fp22_and_the_drain_round_as_the_accumulator_does():
    assert p6.fp22(1 + 2.0**-14) == 1 + 2.0**-14
    assert p6.fp22(1 + 2.0**-15) == 1.0
    assert p6.fp22(1 + 3 * 2.0**-15) == 1 + 2.0**-13
    got = p6.drained(np.array([1e9, -1e9, 1e-9, -1e-9, 1.0, 2.0**-14, 1 + 2.0**-11]))
    assert got.tolist() == [65504.0, -65504.0, 0.0, 0.0, 1.0, 2.0**-14, 1.0]
    assert not np.signbit(got[3])


def test_a_pair_truncates_the_smaller_exponent_sum():
    x = np.zeros((4, 64), np.float16)
    w = np.zeros((4, 64), np.float16)
    # Block 0 sums 63 at exponent sum 40; block 1's 61 * 63 at 24 shifts by 16.
    x[0, 0], x[0, 1], w[0, 1] = 63, 1, 63
    x[0, 32], w[0, 32] = 61 * 2.0**-16, 63
    y = p6.issue(p6.operand(x), p6.operand(w), 0)
    assert y[0, 0] == 63 + 3 / 64
    assert p6.fp22(63 + 61 * 63 * 2.0**-16) != y[0, 0]
    assert not y[1:].any() and not y[0, 1:].any()


def test_the_witness_sums_are_the_exact_integer_product():
    for shape in ("t4", "s200x192"):
        m, n, _, _ = p6.SHAPES[shape]
        ins = p6.data("witness", m, n)
        exact = ins["x"].astype(np.float64) @ ins["w"].astype(np.float64).T
        assert np.abs(exact).max() < 2**11
        *_, tree = p6.tree(shape, "witness", p6.TREE, "")
        *_, unsplit = p6.unsplit(shape, "witness", 0)
        assert np.array_equal(tree(ins)["y2"], exact), shape
        assert np.array_equal(unsplit(ins)["y"], exact), shape


def test_the_f16_witness_is_the_exact_sum_with_bias_and_residual():
    m, n, _, _ = p6.SHAPES["s200x192"]
    ins = p6.data("witness", m, n)
    *_, want = p6.partials("s200x192", "witness", list(SYSNODES), 0)
    exact = ins["x"].astype(np.float64) @ ins["w"].astype(np.float64).T
    z = want(ins)["z"]
    assert np.array_equal(z, exact + ins["bias"] + ins["res"])


def test_the_seeded_tree_is_the_quantised_product_to_fp22_precision():
    m, n, _, _ = p6.SHAPES["s200x192"]
    ins = p6.data("seeded", m, n)
    q = (
        mxfp7.value_fp16(mxfp7.lift(ins["x"], 2))
        @ mxfp7.value_fp16(mxfp7.lift(ins["w"], 2)).T
    )
    *_, want = p6.tree("s200x192", "seeded", p6.TREE, "")
    y = want(ins)["y2"]
    assert np.abs(y - q).max() <= 2.0**-9 * np.abs(q).max()


def test_each_receiver_rings_ready_before_its_receive_and_each_contributor_waits():
    _, src, *_ = p6.tree("t4", "seeded", p6.TREE, "payload")
    blocks = src.split("    node ")[1:]
    by = {int(b.split("\n")[0]): b for b in blocks}
    assert by[2].index("ring mesh=3 slot=1") < by[2].index("fill A0 recv")
    assert by[2].index("ring mesh=1 slot=1") > by[2].index("fill A0 recv")
    one = by[1]
    assert one.index("fill A0 recv") < one.index("wait slot 1") < one.index("settle")
    assert one.index("settle") < one.index("buf=2")
    assert one.index("buf=2") < one.index("count")
