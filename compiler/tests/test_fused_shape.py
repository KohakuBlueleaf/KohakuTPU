"""The fused row-wise kernels, and what they cost now that they run.

`kohakutpu/kernels/fused.py` writes softmax, rmsnorm, layernorm and group_norm
in the shape the fused kernels argue for: one tiling, no
full-shape temp, the chain as a sequence.

EVERY ASSERTION HERE USED TO BE A REFUSAL, and what is pinned now is the
arithmetic and the cost. The two refusals left are the ones that would be
silently WRONG: a fold with no row width, and two row widths in one expression.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, units
from kohakutpu.kernels import fused
from kohakutpu.lang import kernel
from kohakutpu.lang.backend import BACKEND
from kohakutpu.lang.errors import LangError
from kohakutpu.model import SimDevice

from kohakutpu import lang as L

FP16 = np.float16
EPS = 1e-5
LOG2E = 1.4426950408889634

M, N = dims("M, N")


# The staged twins these are graded against. FIXTURES, defined here: they were
# deleted from the library once the fused forms replaced them.
@kernel
def softmax_staged(x=L.In(..., M, N), y=L.Out(..., M, N)):
    """One pass per intermediate, each landing in DRAM."""
    hi, ex, total = L.temp(M, N), L.temp(M, N), L.temp(M, N)
    hi <<= L.row_max(x)
    ex <<= L.exp2((x - hi) * LOG2E)
    total <<= L.row_sum(ex)
    y <<= ex / total


@kernel
def rmsnorm_staged(x=L.In(..., M, N), w=L.In(..., M, N), y=L.Out(..., M, N)):
    """`x / sqrt(mean(x^2) + eps) * w`, dividing before squaring.

    The sum reaches fp16's ceiling at full width, which is why the staged form
    scales first and the fused one does not have to.
    """
    sq, ms = L.temp(M, N), L.temp(M, N)
    sq <<= (x / N) * x
    ms <<= L.row_sum(sq)
    y <<= x * L.rsqrt(ms + EPS) * w


@kernel
def layernorm_staged(
    x=L.In(..., M, N), w=L.In(..., M, N), b=L.In(..., M, N), y=L.Out(..., M, N)
):
    """`(x - mean) * rstd * w + b`, every intermediate a full-shape temp.

    Split to the descriptor budget: a chain reads at most three operands, so the
    gain and the shift cannot ride the same statement as the deviation.
    """
    mu, dev, sq, var, norm = (L.temp(M, N) for _ in range(5))
    mu <<= L.row_sum(x) / N
    dev <<= x - mu
    sq <<= (dev / N) * dev
    var <<= L.row_sum(sq)
    norm <<= dev * L.rsqrt(var + EPS)
    y <<= norm * w + b


#: The fused kernel to the staged twin it replaced.
STAGED = {
    "softmax_fused": softmax_staged,
    "rmsnorm_fused": rmsnorm_staged,
    "layernorm_fused": layernorm_staged,
    "group_norm_fused": layernorm_staged,
}


def operands(shape=(32, 128), seed=0):
    rng = np.random.default_rng(seed)
    x = np.asarray(rng.standard_normal(shape), FP16)
    w = np.asarray(rng.standard_normal(shape) * 0.1 + 1, FP16)
    return x, w, np.asarray(rng.standard_normal(shape) * 0.1, FP16)


def softmax_of(x):
    e = np.exp(np.float64(x) - np.float64(x).max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def rmsnorm_of(x, w):
    x = np.float64(x)
    return x / np.sqrt((x**2).mean(axis=1, keepdims=True) + EPS) * np.float64(w)


def layernorm_of(x, w, b):
    d = np.float64(x) - np.float64(x).mean(axis=1, keepdims=True)
    scale = 1.0 / np.sqrt((d**2).mean(axis=1, keepdims=True) + EPS)
    return d * scale * np.float64(w) + np.float64(b)


#: Name, operand count, reference. `group_norm_fused` takes the group to be the
#: row, which is layernorm's arithmetic over a different axis.
CASES = [
    ("softmax_fused", 1, softmax_of),
    ("rmsnorm_fused", 2, rmsnorm_of),
    ("layernorm_fused", 3, layernorm_of),
    ("group_norm_fused", 3, layernorm_of),
]

SHAPES = [(32, 128), (64, 64)]


def flits_of(kern, arrays):
    """``(stages, instruction words, folded DRAM scalars)`` for one compilation."""
    dev = SimDevice(size=64 << 20)
    compiled = kern.plan(*[dev.tensor(a) for a in arrays])
    consts = BACKEND.constants(compiled) or {}
    names = list(compiled.layouts) + list(consts)
    addrs = {n: 0x100000 * (i + 1) for i, n in enumerate(sorted(names))}
    words = sum(
        len(w)
        for stage in compiled.stages
        for w in BACKEND.encode(compiled, stage, addrs).values()
    )
    return len(compiled.stages), words, len(consts)


@pytest.mark.parametrize("shape", SHAPES, ids=str)
@pytest.mark.parametrize(("name", "nargs", "want"), CASES, ids=[c[0] for c in CASES])
def test_the_fused_shape_is_one_stage_and_computes_the_right_numbers(
    name, nargs, want, shape
):
    """The whole point: one stage, graded on the vector model against numpy.

    An image that merely assembles proves nothing -- a wrong operand slot
    assembles perfectly. The tolerance is relative to the reference's own scale,
    since a row sum of 128 fp16 terms carries several ulps by itself.
    """
    args = operands(shape)[:nargs]
    stages, _, _ = flits_of(getattr(fused, name), args)
    assert stages == 1, f"{name} split into {stages} stages"
    dev = SimDevice(size=64 << 20)
    got = getattr(fused, name)(*[dev.tensor(a) for a in args]).numpy()
    truth = want(*args)
    error = np.abs(np.float64(got) - truth).max() / np.abs(truth).max()
    assert error < 2e-3, f"{name} at {shape}: {error:.2e}"


@pytest.mark.parametrize(("name", "nargs", "want"), CASES, ids=[c[0] for c in CASES])
def test_the_fused_form_costs_less_than_the_staged_one(name, nargs, want):
    """Measured at 64x64: softmax 974 -> 290 words, layernorm 1254 -> 416.

    The staged forms allocate a full-shape temp per intermediate and make a
    pass per statement, so this is the whole claim of the fused shape.
    """
    from kohakutpu import kernels as K

    args = operands((64, 64))[:nargs]
    assert getattr(K, name.removesuffix("_fused")) is getattr(fused, name)
    was_stages, was_words, _ = flits_of(STAGED[name], args)
    now_stages, now_words, _ = flits_of(getattr(fused, name), args)
    assert now_stages < was_stages
    assert now_words < was_words, f"{name}: {was_words} -> {now_words}"


def test_no_intermediate_of_a_fused_kernel_reaches_memory():
    """One drain and no temp: `dev`, `var` and `ex` exist only in registers.

    A chain lifted out of an expression was never a buffer, so eliding its store
    is not an analysis and carries none of the stale-read risk that eliding a
    STATEMENT's store would.
    """
    from kohakutpu.lang import backend as B

    args = operands((32, 128))
    dev = SimDevice(size=64 << 20)
    compiled = fused.layernorm_fused.plan(*[dev.tensor(a) for a in args])
    band, _, _ = B._build(compiled, list(compiled.stages[0].instances[0].stmts))
    assert compiled.temps == {}
    assert band.nout == 1 and len(band.chains) == 2
    assert len(band.held) == 1, "the deviation should live in one register"


@pytest.mark.parametrize(
    ("name", "nargs", "descriptors"),
    [
        ("softmax_fused", 1, 6),
        ("rmsnorm_fused", 2, 6),
        ("layernorm_fused", 3, 8),
        ("group_norm_fused", 3, 8),
    ],
)
def test_each_kernel_fits_the_eight_descriptors(name, nargs, descriptors):
    """`2*nin + 1 + nout`, and layernorm lands at exactly 8 of 8.

    The budget is what constants in registers buy: with `N` and `eps` filled
    from DRAM instead, rmsnorm needs 10 and layernorm 12.
    """
    from kohakutpu.lang import backend as B

    args = operands((32, 128))[:nargs]
    dev = SimDevice(size=64 << 20)
    compiled = getattr(fused, name).plan(*[dev.tensor(a) for a in args])
    band, _, _ = B._build(compiled, list(compiled.stages[0].instances[0].stmts))
    assert 2 * band.nin + 1 + band.nout == descriptors
    assert band.vl == 128, "a reducing band steps one ROW, not VLMAX"


def test_a_reduction_composes_and_lowers():
    """B10's smallest case, which now runs: `row_sum(x) * 0.5` is one band.

    Before B10 this stopped at `TypeError: unsupported operand type(s) for *:
    'Reduction' and 'float'` -- the author could not spell it. Then the emitter
    refused it for having no row width. It has one, off the port's shape.
    """
    from kohakuaccel.lang import dims
    from kohakutpu.lang import kernel

    from kohakutpu import lang as L

    M, N = dims("M, N")

    @kernel
    def scaled(x=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
        with units(x.parts(part)) as e:
            y[e] <<= L.row_sum(x[e]) * 0.5

    x, _, _ = operands((64, 64))
    got = scaled(SimDevice(size=64 << 20).tensor(x)).numpy()
    want = np.repeat(np.float64(x).sum(axis=1, keepdims=True) * 0.5, 64, axis=1)
    assert np.abs(np.float64(got) - want).max() / np.abs(want).max() < 3e-3


@pytest.mark.parametrize(("name", "nargs", "want"), CASES, ids=[c[0] for c in CASES])
def test_no_operand_reaches_an_undemonstrated_slot(name, nargs, want):
    """A folded constant is a VECTOR register, so every ALU source is `SRC_V`.

    Guessing an operand slot produces a legal word that computes something else,
    which is the one failure an assembled image cannot show. Seeding through a
    VBCAST means no chain here emits a `K` or `S` ALU source at all.
    """
    from kohakutpu.hw import vector as V
    from kohakutpu.isa.vecemit import BINARY, UNARY
    from kohakutpu.lang import backend as B

    # Only an ALU word reads `sa`/`sb`/`sc`; VLD and VFILL put other fields there.
    alu = {*UNARY.values(), *(op for op, _ in BINARY.values()), "VMUL", "VINV"}
    args = operands((32, 128))[:nargs]
    dev = SimDevice(size=64 << 20)
    compiled = getattr(fused, name).plan(*[dev.tensor(a) for a in args])
    band, _, _ = B._build(compiled, list(compiled.stages[0].instances[0].stmts))
    names = {code: op for op, code in V.OPS.items()}
    seen, immediate = 0, False
    for word in band.image:
        op = None if immediate else names.get((word >> 27) & 0x1F)
        immediate = op == "VSETI"
        if op not in alu:
            continue
        seen += 1
        sels = [(word >> shift) & 3 for shift in (25, 23, 21)]
        assert sels == [V.SRC_V] * 3, f"{op} reads a non-vector operand: {sels}"
    assert seen, "no ALU word was checked"


def test_a_batched_port_is_still_one_stage():
    """A leading `...` becomes a grid axis, and the band is per batch element."""
    rng = np.random.default_rng(4)
    x = np.asarray(rng.standard_normal((4, 32, 128)), FP16)
    stages, _, _ = flits_of(fused.softmax_fused, [x])
    assert stages == 1
    got = fused.softmax_fused(SimDevice(size=64 << 20).tensor(x)).numpy()
    e = np.exp(np.float64(x) - np.float64(x).max(axis=-1, keepdims=True))
    want = e / e.sum(axis=-1, keepdims=True)
    assert np.abs(np.float64(got) - want).max() / want.max() < 2e-3


# ------------------------------------------------------------------- refusals
def test_a_part_that_CUTS_A_ROW_refuses_and_names_the_knob():
    """A band steps whole rows, so a part that is not whole rows cannot run.

    An instance no longer has to hold the whole array -- a row is folded by one
    step, so splitting rows across instances is the same arithmetic. What is
    still refused is a part that cuts a row, which would fold two into one and
    report success. The message has to say which knob.
    """
    args = operands((128, 128))
    dev = SimDevice(size=64 << 20)
    with pytest.raises(LangError, match=r"raise `part=`"):
        fused.layernorm_fused(*[dev.tensor(a) for a in args], part=200)
    dev = SimDevice(size=64 << 20)
    got = fused.layernorm_fused(*[dev.tensor(a) for a in args], part=128 * 128)
    want = layernorm_of(*args)
    assert np.abs(np.float64(got.numpy()) - want).max() / np.abs(want).max() < 2e-3


@pytest.mark.parametrize("part", [16384, 8192, 4096, 2048, 128])
def test_splitting_the_rows_across_instances_is_the_SAME_ANSWER(part):
    """1 instance or 128, a folded row lands in exactly one of them.

    The whole warrant for gridding a reducing band: the step is a row, so the
    partition cannot change what any row folds to. Graded rather than assumed,
    because a wrong split is a legal program computing a different function.
    """
    args = operands((128, 128))
    dev = SimDevice(size=64 << 20)
    got = fused.layernorm_fused(*[dev.tensor(a) for a in args], part=part).numpy()
    dev = SimDevice(size=64 << 20)
    whole = fused.layernorm_fused(*[dev.tensor(a) for a in args], part=16384).numpy()
    assert np.array_equal(got, whole), f"part={part} moved the bytes"


def test_a_fold_with_no_row_width_is_refused():
    """The silent wrong answer this guard exists for.

    A one-axis port has no row, so a band would step VLMAX, fold whatever lands
    in those lanes, and report success. `vl` has to come from somewhere.
    """
    from kohakuaccel.lang import dims
    from kohakutpu.lang import kernel

    from kohakutpu import lang as L

    N = dims("N")[0]

    @kernel
    def flat(x=L.In(..., N), y=L.Out(..., N), *, part=8192):
        with units(x.parts(part)) as e:
            y[e] <<= x[e] / L.row_sum(x[e])

    x = np.asarray(np.arange(4096) % 7 + 1, FP16)
    with pytest.raises(LangError, match="fold two rows into one answer"):
        flat(SimDevice(size=64 << 20).tensor(x))


def test_two_row_widths_in_one_expression_are_refused():
    """One program steps ONE row shape, so 32x128 and 64x64 cannot share a band.

    They hold the same number of elements, so nothing downstream would notice:
    the band would fold one of them across the wrong boundary.
    """
    from kohakuaccel.lang import dims
    from kohakutpu.lang import kernel

    from kohakutpu import lang as L

    M, N, P, Q = dims("M, N, P, Q")

    @kernel
    def mixed(a=L.In(..., M, N), b=L.In(..., P, Q), y=L.Out(..., M, N), *, part=8192):
        with units(a.parts(part)) as e:
            y[e] <<= L.row_sum(a[e]) + L.row_sum(b[e])

    dev = SimDevice(size=64 << 20)
    wide = dev.tensor(np.zeros((32, 128), FP16))
    tall = dev.tensor(np.zeros((64, 64), FP16))
    with pytest.raises(LangError, match="one row shape"):
        mixed(wide, tall)


def test_the_staged_forms_still_compute_the_same_thing():
    """The negative control: the fused shape is new, the arithmetic is not."""
    x, _, _ = operands()
    got = softmax_staged(SimDevice(size=64 << 20).tensor(x)).numpy()
    want = softmax_of(x)
    assert np.abs(np.float64(got) - want).max() / want.max() < 2e-3
