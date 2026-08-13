"""A stage is a PROGRAM: several chains in one image, intermediates in registers.

`BandKernel` is the container the old stack called a band. What matters is that
it computes the same numbers as the separate programs it replaces, so every test
here runs it on the vector model and grades it -- an image that merely assembles
proves nothing, which is how a wrong operand slot has always got through.

The byte-identity witness for the shipped kernels is `test_band_witness.py`.
"""

import numpy as np
import pytest
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import (
    IMEM_WORDS,
    OUT_REG,
    BandKernel,
    Chain,
    ElementwiseKernel,
    VecEmitError,
    forward,
    konst,
)
from kohakutpu.model import Memory, VectorUnit

FP16 = np.float16
#: Far enough apart that no operand's batch reaches the next.
SLAB = 1 << 16


def run(band, arrays, nelem, nout=1):
    """Run `band` over `arrays` on the model. Returns one array per drain."""
    unit, mem = VectorUnit(mem_base=0), Memory(1 << 22)
    srcs = [(i + 1) * SLAB for i in range(len(arrays))]
    for at, a in zip(srcs, arrays, strict=True):
        mem.write(at, np.asarray(a, FP16).tobytes())
    dsts = [(len(arrays) + 1 + k) * SLAB for k in range(nout)]
    for flit in band.flits(srcs, dsts, nelem):
        unit.execute(flit, mem)
    return [
        np.frombuffer(mem.read(d, nelem * 2), FP16).astype(np.float64) for d in dsts
    ]


def rows(seed, r, c, scale=2.0):
    rng = np.random.default_rng(seed)
    return np.asarray(rng.standard_normal((r, c)) * scale, FP16).astype(np.float64)


def close(got, want, tol=3e-3) -> bool:
    """Error relative to the reference's own scale, as the suite grades it.

    An absolute epsilon is meaningless across these shapes: a reduction over 64
    rows lands near 400, where one fp16 ulp is already 0.25.
    """
    got = np.asarray(got, np.float64).reshape(-1)
    want = np.asarray(want, np.float64).reshape(-1)
    return bool(np.abs(got - want).max() / np.abs(want).max() < tol)


# --------------------------------------------------------------- the container
@pytest.mark.parametrize(
    "ops,nin",
    [
        ([(OpKind.NEG, [0])], 1),
        ([(OpKind.MUL, [0, 1])], 2),
        ([(OpKind.MUL, [0, 1]), (OpKind.ADD, [OUT_REG, 2])], 3),
        ([(OpKind.DIV, [0, 1])], 2),
        ([(OpKind.SUB, [0, 1]), (OpKind.EXP2, [OUT_REG])], 2),
        ([(OpKind.RMAX, [0])], 1),
        ([(OpKind.SUM, [0]), (OpKind.MUL, [OUT_REG, 1])], 2),
    ],
)
@pytest.mark.parametrize("chunks", [1, 4, 8])
def test_a_band_of_one_chain_is_the_old_kernel_word_for_word(ops, nin, chunks):
    """The witness that makes replacing the per-statement path safe.

    Anything that does not use a new feature must emit a byte-identical
    program, and a band of one storing chain is exactly that case.
    """
    old = ElementwiseKernel(ops, nin, chunks=chunks)
    new = BandKernel([Chain(tuple(ops))], nin, chunks=chunks)
    assert new.image == old.image
    srcs = [0x1000 * (i + 1) for i in range(nin)]
    assert new.flits(srcs, [0x9000], 4096) == old.flits(srcs, 0x9000, 4096)


def test_two_independent_chains_share_one_image_and_one_fill():
    """What a stage of independent statements costs once it is one program."""
    band = BandKernel(
        [
            Chain(((OpKind.MUL, [0, 1]),)),
            Chain(((OpKind.ADD, [0, 1]),)),
        ],
        nin=2,
        chunks=1,
    )
    a, b = rows(1, 1, 128).reshape(-1), rows(2, 1, 128).reshape(-1)
    got = run(band, [a, b], 128, nout=2)
    assert close(got[0], a * b)
    assert close(got[1], a + b)
    # Two programs today: two images, two fills of each operand, two drains.
    apart = ElementwiseKernel([(OpKind.MUL, [0, 1])], 2, chunks=1)
    assert len(band.image) < 2 * len(apart.image)


def test_a_forwarded_result_never_reaches_dram():
    """`hi = rmax(x); y = exp2(x - hi)` -- the softmax head, as one program.

    `hi` is `store=False`, so the band drains only `y`: the intermediate exists
    in a register and in no buffer at all.
    """
    band = BandKernel(
        [
            Chain(((OpKind.RMAX, [0]),), store=False),
            Chain(
                ((OpKind.SUB, [0, forward(0)]), (OpKind.EXP2, [OUT_REG])), store=True
            ),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    assert band.nout == 1
    x = rows(3, 1, 64)
    got = run(band, [x.reshape(-1)], 64)[0]
    want = np.exp2(x - x.max(axis=1, keepdims=True)).reshape(-1)
    assert close(got, want)


def test_the_whole_softmax_is_one_band():
    """Four chains, two reductions, one operand, one drain.

    `softmax` ships as four stages and three full-shape temps; this is the same
    arithmetic with nothing between the ops but registers.
    """
    band = BandKernel(
        [
            Chain(((OpKind.RMAX, [0]),), store=False),
            Chain(
                ((OpKind.SUB, [0, forward(0)]), (OpKind.EXP2, [OUT_REG])), store=False
            ),
            Chain(((OpKind.SUM, [forward(1)]),), store=False),
            Chain(((OpKind.DIV, [forward(1), forward(2)]),), store=True),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    assert band.nout == 1 and len(band.held) == 3
    x = rows(5, 1, 64)
    got = run(band, [x.reshape(-1)], 64)[0]
    e = np.exp2(x - x.max(axis=1, keepdims=True))
    assert close(got, e / e.sum(axis=1, keepdims=True))


def test_a_reduction_folds_exactly_vl_lanes():
    """`vl` is the ROW WIDTH when a chain reduces, and rows are steps.

    Passing VLMAX for a 64-wide row would fold two rows into one answer, which
    is the silent wrong answer this argument exists to prevent.
    """
    x = rows(7, 4, 64)
    band = BandKernel([Chain(((OpKind.RMAX, [0]),))], nin=1, chunks=4, vl=64)
    got = run(band, [x.reshape(-1)], 256)[0].reshape(4, 64)
    assert close(
        got.reshape(4, 64), np.repeat(x.max(axis=1, keepdims=True), 64, axis=1)
    )

    wrong = BandKernel([Chain(((OpKind.RMAX, [0]),))], nin=1, chunks=2, vl=128)
    paired = run(wrong, [x.reshape(-1)], 256)[0].reshape(4, 64)
    assert not np.allclose(paired, got), "VL must be the row width, not VLMAX"


# ------------------------------------------------------------------- refusals
def test_a_band_over_the_descriptor_budget_is_refused():
    """Two per filled operand, ONE store between them all, one drain each."""
    with pytest.raises(VecEmitError, match="descriptors"):
        BandKernel([Chain(((OpKind.ADD, [0, 1]),))] * 2, nin=3, chunks=1)


def test_every_result_shares_one_store_descriptor():
    """What lets four chains fit: the VST offset reaches every output region.

    A descriptor per result would cost `2*nin + 2*nout` and a four-chain
    softmax would need ten. Sharing the store makes it `2*nin + 1 + nout`, so
    the same band is seven and fits.
    """
    band = BandKernel(
        [Chain(((OpKind.MUL, [0, 1]),)), Chain(((OpKind.ADD, [0, 1]),))],
        nin=2,
        chunks=1,
    )
    assert isinstance(band.ad_st, int) and len(band.ad_drain) == 2
    a, b = rows(11, 1, 128).reshape(-1), rows(12, 1, 128).reshape(-1)
    got = run(band, [a, b], 128, nout=2)
    assert close(got[0], a * b)
    assert close(got[1], a + b), "the second store overwrote the first"


def test_a_softmax_that_stores_every_step_still_fits():
    """The band the compiler will build once a stage may hold a dependency.

    Every chain stores, because eliding one needs to know no later stage reads
    it. Four results and one operand is seven descriptors, which fits -- so the
    fused form does not depend on that analysis landing first.
    """
    band = BandKernel(
        [
            Chain(((OpKind.RMAX, [0]),)),
            Chain(((OpKind.SUB, [0, forward(0)]), (OpKind.EXP2, [OUT_REG]))),
            Chain(((OpKind.SUM, [forward(1)]),)),
            Chain(((OpKind.DIV, [forward(1), forward(2)]),)),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    assert band.nout == 4
    x = rows(13, 1, 64)
    got = run(band, [x.reshape(-1)], 64, nout=4)
    e = np.exp2(x - x.max(axis=1, keepdims=True))
    assert close(got[3], e / e.sum(axis=1, keepdims=True))


# ------------------------------------------- values that live across a VRED
def test_an_operand_survives_a_reduction():
    """`s = sum(x); y = x - s` -- the loaded operand is read AFTER the VRED."""
    band = BandKernel(
        [
            Chain(((OpKind.SUM, [0]),), store=False),
            Chain(((OpKind.SUB, [0, forward(0)]),)),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    x = rows(21, 1, 64)
    got = run(band, [x.reshape(-1)], 64)[0]
    assert close(got, x - x.sum(axis=1, keepdims=True))


def test_a_held_value_survives_a_reduction_that_consumes_it():
    """The layernorm shape in miniature, and the one attention never exercises.

    `a = x*x` is held; a VRED then reduces `a` itself; and `a` is read again
    afterwards. A band built only for attention passes every other test here and
    fails this one, because `row_max` and `row_sum` there are independent.
    """
    band = BandKernel(
        [
            Chain(((OpKind.MUL, [0, 0]),), store=False),
            Chain(((OpKind.SUM, [forward(0)]),), store=False),
            Chain(((OpKind.ADD, [forward(0), forward(1)]),)),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    x = rows(22, 1, 64, scale=0.5)
    got = run(band, [x.reshape(-1)], 64)[0]
    sq = x * x
    assert close(got, sq + sq.sum(axis=1, keepdims=True))


def test_a_second_reduction_consumes_the_first_ones_result():
    """`t = sum(x); d = x - t; v = sum(d); y = d * v` -- layernorm's structure.

    Two VREDs where the second reduces a value derived from the first, and `d`
    lives across the second. This is what `layernorm_fused` needs and what
    `rmsnorm_fused` and `group_norm_fused` need too.
    """
    band = BandKernel(
        [
            Chain(((OpKind.SUM, [0]),), store=False),
            Chain(((OpKind.SUB, [0, forward(0)]),), store=False),
            Chain(((OpKind.SUM, [forward(1)]),), store=False),
            Chain(((OpKind.MUL, [forward(1), forward(2)]),)),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    x = rows(23, 1, 64, scale=0.5)
    got = run(band, [x.reshape(-1)], 64)[0]
    d = x - x.sum(axis=1, keepdims=True)
    assert close(got, d * d.sum(axis=1, keepdims=True))


def test_sumsq_squares_its_own_operand():
    """SUMSQ's leaf is `va * vb`, so `vb` must name the register being reduced.

    Left at zero it silently multiplies by whatever register 0 holds, which is
    the loaded operand -- right whenever a band reduces register 0 and wrong the
    moment a forwarded value is reduced instead.
    """
    band = BandKernel(
        [
            Chain(((OpKind.MUL, [0, 0]),), store=False),
            Chain(((OpKind.SUMSQ, [forward(0)]),)),
        ],
        nin=1,
        chunks=1,
        vl=64,
    )
    x = rows(24, 1, 64, scale=0.5)
    got = run(band, [x.reshape(-1)], 64)[0]
    want = ((x * x) ** 2).sum(axis=1, keepdims=True)
    assert close(got, np.repeat(want, 64, axis=1))


# ----------------------------------------------- constants that are not filled
def test_a_folded_constant_costs_a_register_and_no_descriptor():
    """`y = (x + eps) * scale` with both scalars in registers: one operand.

    Filled from DRAM the same band reads three operands and needs nine
    descriptors. Seeded, it reads one and needs four, which is what buys
    `layernorm_fused` its seat at the table.
    """
    band = BandKernel(
        [
            Chain(
                ((OpKind.ADD, [0, konst(0)]), (OpKind.MUL, [OUT_REG, konst(1)])),
            )
        ],
        nin=1,
        chunks=1,
        consts=(0.25, 3.0),
    )
    assert 2 * band.nin + 1 + band.nout == 4
    assert len(band.seeded) == 2
    assert set(band.seeded.values()).isdisjoint(band.held.values())
    x = rows(31, 1, 128)
    got = run(band, [x.reshape(-1)], 128)[0]
    assert close(got, (x + 0.25) * 3.0)


def test_a_folded_constant_reaches_a_slot_a_scalar_operand_cannot():
    """A broadcast constant is a VECTOR register, so every slot takes it.

    `("vc", SRC_S)` has never been demonstrated, so `VADD`'s addend cannot be a
    scalar operand -- and `var + eps` is exactly that shape. Through a VBCAST
    the addend is an ordinary vector source and the question does not arise.
    """
    band = BandKernel(
        [Chain(((OpKind.SUB, [0, konst(0)]),))], nin=1, chunks=1, consts=(1.5,)
    )
    x = rows(32, 1, 128)
    assert close(run(band, [x.reshape(-1)], 128)[0], x - 1.5)


def test_a_band_with_no_constants_emits_the_image_it_always_did():
    """The byte-identity claim: seeding costs nothing when nothing is seeded."""
    ops = [(OpKind.MUL, [0, 1])]
    assert BandKernel([Chain(tuple(ops))], 2, chunks=4, consts=()).image == (
        ElementwiseKernel(ops, 2, chunks=4).image
    )


def test_a_band_that_outruns_instruction_memory_is_refused():
    """A band unrolls every step, so a wide reducing band overflows IMEM first.

    Nothing hit this until a band could reduce: `_chunks` caps the elementwise
    path at eight, while a reducing band steps one row and takes as many steps
    as L1 allows. At 64 steps of a 20-op chain the image is over 512 words.
    """
    ops = tuple((OpKind.MUL, [0, 0]) for _ in range(20))
    with pytest.raises(VecEmitError, match=f"over {IMEM_WORDS} instruction"):
        BandKernel([Chain(ops)], nin=1, chunks=64, vl=16)


def test_a_forward_of_a_chain_that_has_not_run_is_refused():
    """A band hands results forward. Backward is a register read of garbage."""
    with pytest.raises(VecEmitError, match="has not run"):
        BandKernel(
            [
                Chain(((OpKind.MUL, [0, forward(1)]),)),
                Chain(((OpKind.NEG, [0]),), store=False),
            ],
            nin=1,
            chunks=1,
        )


def test_a_band_too_wide_for_l1_is_refused():
    """`require_l1`'s measured bad band, where the card returns wrong data.

    At the eight chunks `_chunks` caps at, the descriptor budget always binds
    first -- four regions of 64 words is 256 and the band starts at 321. It
    takes a wider batch to reach L1 at all, which is why this asks for sixteen.
    """
    with pytest.raises(ValueError, match="L1 words"):
        BandKernel([Chain(((OpKind.ADD, [0, 1]),))], nin=2, chunks=16)


def test_flits_wants_one_address_per_operand_and_per_drain():
    band = BandKernel([Chain(((OpKind.NEG, [0]),))], nin=1, chunks=1)
    with pytest.raises(VecEmitError, match="fills 1 operands and drains 1"):
        band.flits([0x1000, 0x2000], [0x3000], 128)
