"""The format ladder, which is how correctness is judged without a threshold.

THE RULE THIS FILE ENFORCES: an absolute error limit is a property of the
OPERANDS, not of the circuit. Cancelling inputs raise every format's relative
error together, so any fixed limit can be beaten by choosing values -- and a
verdict that can be beaten that way is not measuring the machine.

So the tests here do not check that errors are small. They check that the
JUDGEMENT does not move when the operands are made extreme, and that it does
move when the answer is actually wrong.
"""

import numpy as np
import pytest

from ktpu.hw import bench, formats, mxfp7, sim


def _problem(m=32, k=64, n=32):
    prob = bench.build(m, k, n, ncl=2, preq=(False, False))
    return prob.a, prob.bt


def _lad(a, bt, got):
    return formats.ladder(a, bt, got, extra={"mxfp7 model": mxfp7.model_matmul(a, bt)})


# ---- the ladder itself --------------------------------------------------
def test_a_perfect_circuit_sits_on_its_own_rung():
    a, bt = _problem()
    lad = _lad(a, bt, mxfp7.model_matmul(a, bt))
    assert lad["detach"] == 0.0
    assert lad["excess"] == 0
    assert lad["own"] == "mxfp7 model"


def test_the_two_independent_mxfp7_implementations_agree():
    """`formats.quantise_mx_int(63, E5M3)` and `ktpu.hw.mxfp7` are separate code.

    They are written from the same spec by different routes, so agreement is
    evidence about the spec rather than about either implementation. A drift
    here means one of them has stopped describing the hardware.
    """
    a, bt = _problem()
    lad = _lad(a, bt, mxfp7.model_matmul(a, bt))
    assert lad["cross_check"] == 0.0


def test_noise_detaches_from_every_rung():
    a, bt = _problem()
    rng = np.random.default_rng(0)
    got = rng.standard_normal((a.shape[0], bt.shape[0])) * 100
    lad = _lad(a, bt, got)
    assert lad["detach"] > 100
    assert lad["excess"] > 0


def test_a_lost_subtile_shows_in_the_COUNT_and_not_the_median():
    """The failure a median cannot see, and the reason `excess` exists.

    Sixteen elements of 4,096 corrupted leaves `detach` at exactly zero -- half
    the population is untouched, so the median is untouched. Only the count
    moves, and it moves by exactly the number of elements destroyed.
    """
    a, bt = _problem(64, 64, 64)
    got = mxfp7.model_matmul(a, bt).copy()
    got[0:4, 0:4] = 0.0
    lad = _lad(a, bt, got)
    assert lad["detach"] == 0.0, "a median genuinely cannot see 16 in 4096"
    assert lad["excess"] == 16


# ---- the property the whole design rests on -----------------------------
@pytest.mark.parametrize(
    "scale,what",
    [
        (1e3, "large operands"),
        (1e-3, "small operands"),
        (1e6, "operands that saturate the FP16 output"),
    ],
)
def test_scaling_the_operands_does_not_move_the_verdict(scale, what):
    """A PERFECT circuit must pass at every scale. This is the user's argument.

    `mxfp7` is block-scaled, so scaling the operands is not a no-op -- the
    block exponent moves and at 1e6 the FP16 emission saturates outright. The
    verdict must survive all of it, because none of it is a fault.
    """
    a, bt = _problem()
    a = a * scale
    lad = _lad(a, bt, mxfp7.model_matmul(a, bt))
    assert lad["detach"] < 1.0, f"{what} broke a correct circuit"
    assert lad["excess"] <= 0, f"{what} broke a correct circuit"


def test_cancelling_operands_do_not_move_the_verdict():
    """The case that defeats a fixed limit, on a circuit that is exactly right.

    Zero-mean operands make every output a fully cancelled sum, so the relative
    error of EVERY format rises together -- the native format goes from 3.2e-03
    to 1.5e-02 here. A 1e-3 limit fails this perfect circuit; the ratio does not.
    """
    rng = np.random.default_rng(1)
    a = rng.standard_normal((32, 64)) * 1e3
    bt = rng.standard_normal((32, 64)) * 1e3
    lad = _lad(a, bt, mxfp7.model_matmul(a, bt))

    assert lad["formats"]["int7 + E5M3"]["p50"] > 1e-3, "the fixed limit is beaten"
    assert lad["detach"] < 1.0, "and the scale-free judgement is not"
    assert lad["excess"] <= 0


def test_the_verdict_itself_is_unmoved_by_extreme_operands():
    """End to end through `_verdict`, not just the ladder."""
    a, bt = _problem()
    for scale in (1.0, 1e-4, 1e4):
        aa = a * scale
        got = mxfp7.model_matmul(aa, bt)
        lad = _lad(aa, bt, got)
        v = sim._verdict(True, sim._stats(got, got), sim._stats(got, got), {}, lad)
        assert v["pass"], f"a correct circuit failed at scale {scale}"


# ---- honesty of the statistics ------------------------------------------
def test_an_overflowed_column_does_not_read_as_perfect():
    """Dropping non-finites made a saturated fp16 score 0.000e+00 -- the most
    flattering possible answer for the worst possible result."""
    a, bt = _problem()
    lad = formats.ladder(a * 1e6, bt)
    fp16 = lad["formats"]["fp16"]
    assert fp16["blown"] > 0
    assert fp16["p50"] == float("inf")
    assert fp16["over10"] >= fp16["blown"]


def test_the_ladder_is_a_real_sub_matmul_not_a_sample():
    """Rows of A are rows of C, so a bounded ladder is exact for what it covers."""
    a, bt = _problem(256, 64, 256)
    lad = formats.ladder(a, bt)
    assert (lad["rows"], lad["cols"]) == (formats.LADDER_ROWS, formats.LADDER_COLS)
    assert lad["of"] == [256, 256]
    small = formats.ladder(a[: formats.LADDER_ROWS], bt[: formats.LADDER_COLS])
    assert lad["formats"] == small["formats"]
