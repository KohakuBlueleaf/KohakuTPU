"""Absorbing `log2 e` into the scale above an exp2, and when it must refuse.

The rewrite is only sound where a uniform positive scale can be pushed through
every op between the multiply and its anchor. The refusals matter more than the
successes: folding a cone that escapes changes an answer, and the answer would
still look plausible.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.dsl.nn import attention
from ktpu.ir import FP16
from ktpu.ir.graph import OpKind
from ktpu.passes import lower
from ktpu.passes.scalefold import LOG2_E, absorb, plan
from ktpu.target import VU13P_8CU as T

L, DH, BLOCK = 128, 64, 64


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def attn(causal=False):
    args = [spec(L, DH)] * 3 + ([spec(BLOCK, BLOCK)] if causal else [])
    return D.trace(
        lambda q, k, v, *r: attention(
            q, k, v, block=BLOCK, causal=causal, tri=r[0] if causal else None
        ),
        *args,
    )


def muls_by(graph, value):
    return [
        o
        for o in graph.ops
        if o.kind is OpKind.MUL
        and any(
            graph.producer(v).kind is OpKind.CONST
            and abs(graph.producer(v).attrs["value"] - value) < 1e-12
            for v in o.inputs
        )
    ]


def test_gelu_folds_its_log2e_multiply():
    """`exp(2x)` inside tanh is `exp2(2x * log2 e)`, and the `2 *` above it is
    an anchor the factor can move into."""
    graph = D.trace(D.gelu, spec(64, 64))
    assert muls_by(graph, LOG2_E)
    assert absorb(graph) == 1
    assert muls_by(graph, LOG2_E) == [], "a `* log2 e` survived the fold"


def test_the_scale_grows_by_exactly_the_factor():
    """tanh's inner `2.0 *` must end up holding 2 * log2 e."""
    graph = D.trace(D.gelu, spec(64, 64))
    absorb(graph)
    assert muls_by(graph, 2.0 * LOG2_E), "no anchor carries the folded constant"


def test_it_is_idempotent():
    graph = D.trace(D.gelu, spec(64, 64))
    assert absorb(graph) == 1
    assert absorb(graph) == 0


def test_attention_needs_no_fold_because_the_kernel_already_is_in_log2_space():
    """`nn.attention` folds `log2 e` into `scale` itself, so the pass finds
    nothing. That is the pass working, not the pass being dead -- it exists for
    kernels a user writes the obvious way, and gelu above is one."""
    graph = attn()
    assert muls_by(graph, LOG2_E) == []
    assert absorb(graph) == 0
    assert muls_by(graph, 64**-0.5 * LOG2_E), "the scale is not in log2 space"


def test_the_interned_constant_is_not_mutated_under_its_other_users():
    """A constant is one value per (number, dtype), so `2.0 *` inside tanh and
    the `2.0 *` outside it are the SAME node. Growing it in place would scale a
    multiply that has nothing to do with the exponential."""
    graph = D.trace(lambda x: D.gelu(x), spec(64, 64))
    before = len(muls_by(graph, 2.0))
    absorb(graph)
    survivors = muls_by(graph, 2.0)
    assert len(survivors) == before - 1, "the other user of `2.0` lost its constant"


def test_a_bare_softmax_is_refused_for_want_of_an_anchor():
    """There is no upstream scale to absorb the factor, and inserting one would
    cost the pass it was meant to save."""
    graph = D.trace(lambda x: D.softmax(x), spec(64, 64))
    _folds, _anchors, _scaled, why = plan(graph)
    assert why and any("input" in w for w in why), why
    assert absorb(graph) == 0


def _returned(x, y):
    """`s` is both inside the cone and a result, so scaling it is visible."""
    s = x @ y * 0.125
    return D.exp(s - s.max(axis=-1, keepdim=True)), s


def _reused(x, y):
    """`s` is read again by a multiply, which is not scale covariant."""
    s = x @ y * 0.125
    return D.exp(s - s.max(axis=-1, keepdim=True)) * s


@pytest.mark.parametrize("kern", [_returned, _reused], ids=["output", "non-covariant"])
def test_an_escaping_cone_is_refused(kern):
    """Anything reading a scaled value that is not itself scaled would silently
    receive a different number, so the whole fold has to be abandoned."""
    graph = D.trace(kern, spec(64, 64), spec(64, 64))
    _f, _a, _s, why = plan(graph)
    assert why, "a scaled value that leaves the cone must block the fold"
    assert absorb(graph) == 0


def test_pow2_corr_is_off_and_the_measurement_says_it_should_stay_off():
    """`row_rescale`'s precondition costs more than the feature returns.

    Rounding the running max up is narrow-span work between a wide reduction
    and its wide consumer, so it SPLITS `[mul, rmax, sub, exp2, sum]` into
    three bands and sends `s` back through L1. The band count is the visible
    symptom; +170 instructions on attention full+causal is the price.
    """
    from ktpu.codegen import codegen
    from ktpu.ir.sched import Engine

    def bands_and_insts(on):
        graph = D.trace(
            lambda a, b, c: attention(a, b, c, block=BLOCK, pow2_corr=on),
            *[spec(L, DH)] * 3,
        )
        sched = lower(graph, T)
        prog = codegen(sched, L, DH, DH, T)
        vec = [b for b in sched.bands if b.engine is Engine.VECTOR]
        return len(vec), len(prog.insts)

    off_bands, off_insts = bands_and_insts(False)
    on_bands, on_insts = bands_and_insts(True)
    assert on_bands > off_bands, "the ceil should have split the fused band"
    assert on_insts > off_insts, "the ceil should have cost instructions"
    assert any(
        len(b.ops) >= 5
        for b in lower(
            D.trace(
                lambda a, b, c: attention(a, b, c, block=BLOCK), *[spec(L, DH)] * 3
            ),
            T,
        ).bands
        if b.engine is Engine.VECTOR
    ), "with it off, the softmax band must still be fused"


@pytest.mark.parametrize("causal", [False, True])
def test_folding_does_not_move_the_answer(causal):
    """The equivalence check: same schedule shape, same numbers to float64."""
    from ktpu.codegen import codegen
    from ktpu.interp.mesh import Mesh

    rng = np.random.default_rng(11)
    q, k, v = (rng.standard_normal((L, DH)) * 0.3 for _ in range(3))
    arrays = [q, k, v] + ([np.tril(np.ones((BLOCK, BLOCK)))] if causal else [])

    got = []
    for on in (False, True):
        graph = attn(causal)
        sched = lower(graph, T, absorb_scale=on)
        prog = codegen(sched, BLOCK if causal else L, DH, DH, T)
        mesh = Mesh(prog, T)
        mesh.upload(sched.bands[0], arrays[0], arrays[1])
        names = [graph.producer(x).attrs["name"] for x in graph.inputs]
        for name, arr in list(zip(names, arrays, strict=True))[2:]:
            mesh.bind(name, arr)
        mesh.run()
        got.append(np.concatenate([mesh.result(f"v{o.vid}") for o in graph.outputs]))

    s = (q @ k.T) * DH**-0.5
    if causal:
        rows = np.arange(L)[:, None]
        s = np.where(np.arange(L)[None, :] <= rows, s, -np.inf)
    p = np.exp(s - s.max(axis=-1, keepdims=True))
    want = (p / p.sum(axis=-1, keepdims=True)) @ v

    for out in got:
        assert np.abs(out - want).max() / np.abs(want).max() < 5e-3
    assert np.abs(got[0] - got[1]).max() / np.abs(want).max() < 5e-3
