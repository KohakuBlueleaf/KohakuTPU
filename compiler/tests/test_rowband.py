"""`RowBandKernel`: chains that fold rows, rows across chunks, graded on the vector model.

Every case runs on `VectorUnit` against a float64 reference, at widths on both
sides of VLMAX: the band has no VRED, so a row is any multiple of 16.
"""

import numpy as np
import pytest
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.rowband import RowBandKernel
from kohakutpu.isa.vecemit import OUT_REG, Chain, VecEmitError, forward, konst
from kohakutpu.model import Memory, VectorUnit

FP16 = np.float16
SLAB = 1 << 18


def run(band, arrays, nelem, nout=1):
    unit, mem = VectorUnit(mem_base=0), Memory(1 << 23)
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


def err(got, want) -> float:
    got, want = np.asarray(got).reshape(-1), np.asarray(want).reshape(-1)
    return float(np.abs(got - want).max() / np.abs(want).max())


SOFTMAX = [
    Chain(((OpKind.RMAX, [0]),), store=False),
    Chain(((OpKind.SUB, [0, forward(0)]), (OpKind.EXP2, [OUT_REG])), store=False),
    Chain(((OpKind.SUM, [forward(1)]),), store=False),
    Chain(((OpKind.DIV, [forward(1), forward(2)]),), store=True),
]


def softmax2(x):
    e = np.exp2(x - x.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


@pytest.mark.parametrize("cols", [16, 64, 128, 256, 512])
@pytest.mark.parametrize(
    "rb,chunks,halves", [(8, 1, 1), (8, 2, 2), (4, 3, 1), (1, 2, 1)]
)
def test_softmax_at_any_width(cols, rb, chunks, halves):
    try:
        band = RowBandKernel(SOFTMAX, 1, cols, rb=rb, chunks=chunks, halves=halves)
    except (VecEmitError, ValueError) as exc:
        pytest.skip(f"does not fit: {exc}")
    n = rb * chunks * halves * 2
    x = rows(cols + rb, n, cols)
    got = run(band, [x.reshape(-1)], n * cols)[0]
    assert err(got, softmax2(x)) < 3e-3


def test_rmsnorm_the_way_the_backend_spells_it():
    """`x * rsqrt(sum(x/n * x) + eps)`: a fold in the middle of one chain."""
    cols = 256
    ops = (
        (OpKind.DIV, [0, konst(0)]),
        (OpKind.MUL, [OUT_REG, 0]),
        (OpKind.SUM, [OUT_REG]),
        (OpKind.ADD, [OUT_REG, konst(1)]),
        (OpKind.RSQRT, [OUT_REG]),
        (OpKind.MUL, [0, OUT_REG]),
    )
    band = RowBandKernel([Chain(ops)], 1, cols, chunks=1, consts=(float(cols), 1e-5))
    x = rows(3, 16, cols)
    got = run(band, [x.reshape(-1)], 16 * cols)[0]
    want = x / np.sqrt((x * x).mean(axis=1, keepdims=True) + 1e-5)
    assert err(got, want) < 3e-3


def test_the_attention_softmax_band():
    """The band flash attention runs per key block: three operands, four stores,
    folds whose inputs are a filled operand, a forwarded value, and a recomputed
    per-word value; `top` and `total` are row-constant arrays."""
    m = (OpKind.RMAX, [1]), (OpKind.MAX, [0, OUT_REG])
    chains = [
        Chain((*m, (OpKind.SUB, [0, OUT_REG]), (OpKind.EXP2, [OUT_REG]))),
        Chain((*m, (OpKind.SUB, [1, OUT_REG]), (OpKind.EXP2, [OUT_REG]))),
        Chain(m, store=False),
        Chain(
            (
                (OpKind.SUB, [1, forward(2)]),
                (OpKind.EXP2, [OUT_REG]),
                (OpKind.SUM, [OUT_REG]),
            ),
            store=False,
        ),
        Chain(
            (
                (OpKind.SUB, [0, forward(2)]),
                (OpKind.EXP2, [OUT_REG]),
                (OpKind.MUL, [2, OUT_REG]),
                (OpKind.ADD, [OUT_REG, forward(3)]),
            )
        ),
        Chain(m),
    ]
    cols, n = 64, 32
    band = RowBandKernel(chains, 3, cols, chunks=1, groups=[0, 1, 2, 3])
    rng = np.random.default_rng(9)
    top = (
        np.repeat(rng.standard_normal((n, 1)), cols, axis=1)
        .astype(FP16)
        .astype(np.float64)
    )
    s = rows(4, n, cols)
    tot = (
        np.repeat(rng.random((n, 1)) + 1, cols, axis=1).astype(FP16).astype(np.float64)
    )
    got = run(band, [top.reshape(-1), s.reshape(-1), tot.reshape(-1)], n * cols, nout=4)
    m2 = np.maximum(top, s.max(axis=1, keepdims=True))
    corr = np.exp2(top - m2)
    p = np.exp2(s - m2)
    total = tot * corr + p.sum(axis=1, keepdims=True)
    # Drain groups: [corr], [p], [total], [m2].
    for g, want in zip(got, (corr, p, total, m2), strict=True):
        assert err(g, want) < 3e-3


@pytest.mark.parametrize("kind", [OpKind.SUM, OpKind.SUMSQ, OpKind.RMAX, OpKind.RMIN])
def test_every_fold_kind_broadcasts_its_row(kind):
    cols = 96
    band = RowBandKernel([Chain(((kind, [0]),))], 1, cols, chunks=2)
    x = rows(11, 16, cols, scale=1.0)
    got = run(band, [x.reshape(-1)], 16 * cols)[0].reshape(16, cols)
    ref = {
        OpKind.SUM: x.sum(axis=1),
        OpKind.SUMSQ: (x * x).sum(axis=1),
        OpKind.RMAX: x.max(axis=1),
        OpKind.RMIN: x.min(axis=1),
    }[kind]
    assert err(got, np.repeat(ref[:, None], cols, axis=1)) < 3e-3


def test_a_fold_of_a_row_constant_is_arithmetic():
    """`row_sum(row_max(x))` is cols * max: the second fold never walks a word."""
    cols = 48
    band = RowBandKernel(
        [Chain(((OpKind.RMAX, [0]), (OpKind.SUM, [OUT_REG])))], 1, cols, chunks=1
    )
    x = rows(2, 8, cols, scale=1.0)
    got = run(band, [x.reshape(-1)], 8 * cols)[0].reshape(8, cols)
    assert err(got, np.repeat(cols * x.max(axis=1)[:, None], cols, axis=1)) < 3e-3


def test_no_word_of_the_image_is_a_vred_or_a_mode_switch_but_the_preamble():
    band = RowBandKernel(SOFTMAX, 1, 256, chunks=1)
    ops = [w >> 27 for w in band.image]
    assert 0x13 not in ops
    assert ops.count(0x19) == 1
