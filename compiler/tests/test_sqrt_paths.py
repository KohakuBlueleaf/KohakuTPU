"""The two square roots, and the reason neither can be graded on `SimDevice`.

This ALU has no square root -- `vec_alu.v` carries OP_INV and OP_RSQRT and
nothing else -- so `L.sqrt_approx` composes one from two seeds at two words and
`L.sqrt_newton` refines it at six.

WHAT THE ASSERTIONS BELOW ARE WORTH. `model.py` computes VRSQRT as
`1/np.sqrt(a)` and VINV as `1/a`, EXACTLY, so on this device both paths are
perfect by construction and agreement with numpy pins the ALGEBRA and nothing
else. The accuracy claim lives in `scripts/py/sqrt_paths.py`, which evaluates
the seeds' coefficient ROM and fixed point the way the RTL does; the last two
tests here hold that script to its numbers.
"""

import pathlib
import sys

import numpy as np
import pytest
from kohakuaccel.lang import dims
from kohakutpu.lang import kernel
from kohakutpu.lang.errors import LangError
from kohakutpu.lang.vector import Ref, Value, chain_of, chains_of, leaves_of
from kohakutpu.model import SimDevice

from kohakutpu import lang as L

FP16 = np.float16
M, N = dims("M, N")

#: `scripts/py` is not a package, and `sqrt_paths` imports `vec_tables` beside it.
SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "py"


@kernel
def approx_root(x=L.In(...), y=L.Out(...)):
    """``sqrt(x)`` as VRSQRT then VINV, at any rank."""
    y <<= L.sqrt_approx(x)


@kernel
def newton_root(x=L.In(...), y=L.Out(...)):
    """``sqrt(x)`` with one Newton step on the reciprocal root, at any rank."""
    y <<= L.sqrt_newton(x)


@pytest.fixture(scope="module")
def script():
    """`scripts/py/sqrt_paths.py`, imported off the path it lives on."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import sqrt_paths

        return sqrt_paths
    finally:
        sys.path.remove(str(SCRIPTS))


def operand(shape=(32, 128), seed=0):
    """Strictly positive fp16, since a root of a negative is not the question."""
    rng = np.random.default_rng(seed)
    return np.asarray(np.abs(rng.standard_normal(shape)) + 0.25, FP16)


def rel(got, want) -> float:
    """Largest error as a fraction of the reference's own peak."""
    want = np.asarray(want, np.float64)
    return float(np.abs(np.asarray(got, np.float64) - want).max() / np.abs(want).max())


def words_of(build):
    """Instruction words `build`'s expression lowers to, lifted chains included."""
    expr = build(Value(Ref("x"))).expr
    lifted, final = chains_of(expr, leaves_of(expr))
    return sum(len(chain) for chain in (*lifted, final))


# ------------------------------------------------------- the algebra, on device
@pytest.mark.parametrize("kern", [approx_root, newton_root], ids=["approx", "newton"])
def test_both_paths_compute_a_square_root(kern):
    """Graded against numpy, which on this model only pins that the algebra is right."""
    x = operand()
    dev = SimDevice(size=64 << 20)
    got = kern(dev.tensor(x)).numpy()
    assert rel(got, np.sqrt(np.float64(x))) < 2e-3


@pytest.mark.parametrize(
    ("build", "words"),
    [(L.sqrt_approx, 2), (L.sqrt_newton, 6)],
    ids=["approx", "newton"],
)
def test_each_path_costs_the_words_it_claims(build, words):
    """Two and six. The whole basis for choosing between them at the call."""
    assert words_of(build) == words


def test_the_model_computes_both_seeds_EXACTLY():
    """Why no test above can grade accuracy: the seeds have no error here at all."""
    from kohakutpu.model import LANE_VALUE

    v = np.array([0.3, 1.0, 2.0, 7.5], np.float64)
    assert np.array_equal(LANE_VALUE["VRSQRT"](v, 0, 0), 1.0 / np.sqrt(v))
    assert np.array_equal(LANE_VALUE["VINV"](v, 0, 0), 1.0 / v)


def test_only_the_two_word_path_fits_a_fused_epilogue():
    """`chain_of` keeps ONE running result, and Newton reads `x*y` twice.

    A real limit on where the refined root can be used, not a quirk of this test:
    `ResidentEpilogueKernel` is the fused-epilogue path and it has nowhere to
    hold a lifted subexpression.
    """
    fast = L.sqrt_approx(Value(Ref("x"))).expr
    assert len(chain_of(fast, leaves_of(fast))) == 2
    slow = L.sqrt_newton(Value(Ref("x"))).expr
    with pytest.raises(LangError, match="ONE running result"):
        chain_of(slow, leaves_of(slow))


# --------------------------------------------- the accuracy claim, off the card
def test_the_silicon_seed_model_reproduces_the_ALUs_own_table_error(script):
    """What makes the script creditable: its seeds land on the bound the ROM implies.

    `vec_tables` measures the polynomial in ABSOLUTE terms against `g` in
    (0.5, 1]; the assembled result is normalised to [1, 2), so half an ulp plus
    that error scaled by 2^16 is the whole budget. Agreement says the normalise,
    round and exponent-bound modelled here add nothing of their own.
    """
    x = script.sweep(4)
    for name, (got, bound) in script.check_seeds(x).items():
        assert got <= bound, f"{name}: {got:.3f} over its {bound:.3f} bound"
        assert got > 0.5, f"{name}: {got:.3f} is at or under a correct rounding"


def test_newton_buys_under_a_tenth_of_an_ulp_for_four_more_words(script):
    """The measurement the choice rests on. Full sweep: 1.556 -> 1.467 ulp.

    The seed is already within 0.07 ulp of correctly rounded, so the
    correction's own five roundings cost about what they remove. Bounds rather
    than the exact figures, which move with the sweep width.
    """
    x = script.sweep(4)
    want = np.sqrt(script.from_e8(x))
    fast, _ = script.ulp_error(script.path_approx(x), want)
    slow, _ = script.ulp_error(script.path_newton(x), want)
    exact, _ = script.ulp_error(script.to_e8(want), want)
    assert exact == pytest.approx(0.5, abs=1e-3), "a correct rounding is half an ulp"
    assert 1.0 < slow < fast < 2.0, f"approx {fast:.3f}, newton {slow:.3f}"
    assert fast - slow < 0.25, f"Newton bought {fast - slow:.3f} ulp for four words"


def test_a_crippled_seed_still_converges(script):
    """That the step is a real Newton step, which the tiny gain above could hide."""
    steps = script.converge(script.sweep(2)[::64], bits=4)
    assert steps[0] > 8.0, f"a 4-bit seed should start badly, got {steps[0]:.1f} ulp"
    assert steps[-1] < 2.0, f"and land at the rounding floor, got {steps[-1]:.1f} ulp"
    assert steps[1] < steps[0] / 4.0, "one step should be quadratic, not linear"
