"""Flash attention and MoE: DSL -> level 1 -> level 2 -> level 3.

These are the acceptance kernels. Flash attention proves carried state through a
compile-time loop; MoE proves compile-time routing. Each is checked numerically
against numpy, then lowered and code-generated to prove the whole chain runs.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.dsl.nn import attention, moe_dense, moe_grouped
from ktpu.interp import run_one
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

RNG = np.random.default_rng(11)
T, C, H = 128, 64, 128


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def np_attention(q, k, v, scale):
    s = (q @ k.T) * scale
    e = np.exp(s - s.max(-1, keepdims=True))
    return (e / e.sum(-1, keepdims=True)) @ v


def np_silu(x):
    return x / (1.0 + np.exp(-x))


def np_softmax(x):
    e = np.exp(x - x.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


# ---------------------------------------------------------------------------
# flash attention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block", [16, 32, 64, 128])
def test_attention_matches_plain_attention(block):
    scale = C**-0.5
    g = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), scale, block=block)
    q, k, v = (RNG.normal(size=(T, C)) for _ in range(3))
    got = run_one(g, q, k, v)
    assert np.allclose(got, np_attention(q, k, v, scale), rtol=1e-9, atol=1e-12)


def test_attention_survives_logits_that_would_overflow():
    """The online max is what keeps exp2 in range; without it this is inf/inf."""
    scale = 1.0
    g = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), scale, block=32)
    q = RNG.normal(size=(T, C)) * 30.0
    k = RNG.normal(size=(T, C)) * 30.0
    v = RNG.normal(size=(T, C))
    got = run_one(g, q, k, v)
    assert np.isfinite(got).all()
    assert np.allclose(got, np_attention(q, k, v, scale), rtol=1e-7, atol=1e-9)


def test_attention_never_materialises_the_full_score_matrix():
    """The point of the algorithm: no value is ever T x T."""
    g = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), C**-0.5, block=32)
    assert all(op.out.shape != (T, T) for op in g.ops)
    widest = max(op.out.numel for op in g.ops)
    assert widest <= T * max(C, 32)


def test_attention_block_count_changes_the_graph():
    a = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), 1.0, block=32)
    b = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), 1.0, block=64)
    assert len(a.ops) > len(b.ops)


def test_attention_rejects_an_indivisible_block():
    with pytest.raises(ValueError, match="does not divide"):
        D.trace(attention, spec(T, C), spec(T, C), spec(T, C), 1.0, block=48)


def test_attention_lowers_and_codegens():
    g = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), C**-0.5, block=64)
    sched = lower(g, VU13P_8CU)
    assert any(b.engine.value == "matmul" for b in sched.bands)
    assert any(b.engine.value == "vector" for b in sched.bands)
    prog = codegen(sched, T, C, C, VU13P_8CU)
    assert prog.insts
    assert prog.memory.total > 0


def _flash_at_level_3(t_len: int, c: int, block: int):
    """Trace, lower, codegen and EXECUTE flash attention; return (got, want)."""
    import numpy as np

    from ktpu.interp.mesh import Mesh

    rng = np.random.default_rng(0)
    q, k, v = (rng.standard_normal((t_len, c)) * 0.3 for _ in range(3))
    g = D.trace(
        attention,
        spec(t_len, c),
        spec(t_len, c),
        spec(t_len, c),
        c**-0.5,
        block=block,
    )
    sched = lower(g, VU13P_8CU)
    mesh = Mesh(codegen(sched, t_len, c, c, VU13P_8CU), VU13P_8CU)
    mesh.upload(sched.bands[0], q, k)
    mesh.bind("v", v)
    mesh.run()
    return mesh.result(), run_one(g, q, k, v)


def test_attention_executes_at_level_3():
    """One block: the whole per-block path -- q @ k.T through a sliced and
    transposed operand, row max, exp, row sum, p @ v with A in L1-ENTRY order
    and B packed -- run as instructions and checked against float64."""
    import numpy as np

    got, want = _flash_at_level_3(64, 64, 64)
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-3


@pytest.mark.parametrize("t_len", [128, 256])
def test_attention_online_softmax_executes_at_level_3(t_len):
    """Several blocks, so the running max and denominator cross bands and the
    accumulator is rescaled by exp2(m_old - m_new) -- the part no scheduler
    derives and the reason the kernel is written by hand.

    The bound is loose in absolute terms because the error grows with the block
    count: 1.2e-3 at one block, 2.0e-3 at two, 2.5e-3 at four. That growth is
    E8M15 accumulating, not a defect -- a layout mistake here reads 1e-1.
    """
    import numpy as np

    got, want = _flash_at_level_3(t_len, 64, 64)
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-3


def test_attention_reads_the_right_block_of_k():
    """`k[64:128].T` is a slice AND a transpose. Folding either away reads the
    wrong elements at the right size, which is why the first `q @ k_block.T`
    was wrong by 4.2 absolute while every shape looked correct.

    The slice becomes an element OFFSET on the operand's address and the
    transpose is absorbed by the packer, so the blocks must land on distinct,
    ascending addresses."""
    g = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), C**-0.5, block=64)
    sched = lower(g, VU13P_8CU)
    mm = [b for b in sched.bands if b.engine.value == "matmul"]
    offs = [b.ops[0].offs[1] for b in mm]
    assert len(set(offs)) > 1, "every block read the same slice of K"
    assert any(b.ops[0].attrs.get("bflip") for b in mm), "the transpose vanished"


# ---------------------------------------------------------------------------
# MoE
# ---------------------------------------------------------------------------


def test_moe_dense_matches_numpy():
    n_exp = 4
    w1 = [spec(C, H) for _ in range(n_exp)]
    w2 = [spec(H, C) for _ in range(n_exp)]
    g = D.trace(moe_dense, spec(T, C), spec(C, n_exp), w1, w2)

    x = RNG.normal(size=(T, C))
    wg = RNG.normal(size=(C, n_exp))
    a = [RNG.normal(size=(C, H)) for _ in range(n_exp)]
    b = [RNG.normal(size=(H, C)) for _ in range(n_exp)]
    got = run_one(g, x, wg, *a, *b)

    gate = np_softmax(x @ wg)
    want = sum(np_silu(x @ a[e]) @ b[e] * gate[:, e : e + 1] for e in range(n_exp))
    assert np.allclose(got, want, rtol=1e-9)


@pytest.mark.parametrize("n_exp", [4, 8, 16])
def test_moe_gate_column_reads_through_the_tile_layout(n_exp):
    """`gate[:, e:e+1]` is a strided column read, and the region it reads is in
    SUB-TILE order, not row-major. Indexing it as `row * E + e` is right only
    when `E == lanes`, which is why E=4 was exact and E=8 was 76% wrong.

    Parametrised past E=4 for exactly that reason: one expert count cannot
    distinguish a correct index from a coincidence.
    """
    from ktpu.interp.mesh import Mesh

    rng = np.random.default_rng(0)
    x = rng.standard_normal((C, C)) * 0.3
    wg = rng.standard_normal((C, n_exp)) * 0.3
    a = [rng.standard_normal((C, C)) * 0.3 for _ in range(n_exp)]
    b = [rng.standard_normal((C, C)) * 0.3 for _ in range(n_exp)]

    g = D.trace(
        moe_dense,
        spec(C, C),
        spec(C, n_exp),
        [spec(C, C)] * n_exp,
        [spec(C, C)] * n_exp,
    )
    sched = lower(g, VU13P_8CU)
    mesh = Mesh(codegen(sched, C, C, C, VU13P_8CU), VU13P_8CU)
    arrays = [x, wg, *a, *b]
    names = [g.producer(v).attrs["name"] for v in g.inputs]
    mesh.upload(sched.bands[0], x, wg)
    for name, arr in list(zip(names, arrays, strict=True))[2:]:
        mesh.bind(name, arr)
    mesh.run()

    want = run_one(g, *arrays)
    assert np.abs(mesh.result() - want).max() / np.abs(want).max() < 5e-3


def test_moe_dense_executes_at_level_3():
    """Compile-time routing, run as instructions. The gate is a `(T, E)` softmax
    whose per-expert weight is `gate[:, e:e+1]` -- a single COLUMN, which is one
    value per row, so it lowers to a row broadcast at stride E rather than a
    materialised operand.

    The gate matmul is `T x C x E` while the experts are `T x C x H`, so this is
    the first kernel with matmuls of DIFFERENT tile shapes -- the reason a
    region's geometry has to travel with it instead of being read off the last
    matmul.
    """
    from ktpu.interp.mesh import Mesh

    n_exp = 4
    rng = np.random.default_rng(0)
    x = rng.standard_normal((C, C)) * 0.3
    wg = rng.standard_normal((C, n_exp)) * 0.3
    a = [rng.standard_normal((C, C)) * 0.3 for _ in range(n_exp)]
    b = [rng.standard_normal((C, C)) * 0.3 for _ in range(n_exp)]

    g = D.trace(
        moe_dense,
        spec(C, C),
        spec(C, n_exp),
        [spec(C, C)] * n_exp,
        [spec(C, C)] * n_exp,
    )
    sched = lower(g, VU13P_8CU)
    mesh = Mesh(codegen(sched, C, C, C, VU13P_8CU), VU13P_8CU)
    arrays = [x, wg, *a, *b]
    names = [g.producer(v).attrs["name"] for v in g.inputs]
    mesh.upload(sched.bands[0], x, wg)
    for name, arr in list(zip(names, arrays, strict=True))[2:]:
        mesh.bind(name, arr)
    mesh.run()

    want = run_one(g, *arrays)
    assert np.abs(mesh.result() - want).max() / np.abs(want).max() < 5e-3


def test_moe_dense_unrolls_one_ffn_per_expert():
    for n_exp in (2, 4):
        g = D.trace(
            moe_dense,
            spec(T, C),
            spec(C, n_exp),
            [spec(C, H)] * n_exp,
            [spec(H, C)] * n_exp,
        )
        # one gate matmul plus two per expert
        assert sum(o.kind.value == "matmul" for o in g.ops) == 1 + 2 * n_exp


def test_moe_grouped_matches_numpy():
    counts = [48, 32, 48]
    w1 = [spec(C, H) for _ in counts]
    w2 = [spec(H, C) for _ in counts]
    g = D.trace(moe_grouped, spec(T, C), w1, w2, counts)

    x = RNG.normal(size=(T, C))
    a = [RNG.normal(size=(C, H)) for _ in counts]
    b = [RNG.normal(size=(H, C)) for _ in counts]
    got = run_one(g, x, *a, *b)

    want, row = [], 0
    for e, n in enumerate(counts):
        want.append(np_silu(x[row : row + n] @ a[e]) @ b[e])
        row += n
    assert np.allclose(got, np.concatenate(want, axis=0), rtol=1e-9)


def test_moe_grouped_rejects_counts_that_do_not_cover_the_rows():
    with pytest.raises(ValueError, match="counts sum to"):
        D.trace(moe_grouped, spec(T, C), [spec(C, H)], [spec(H, C)], [T - 1])


def test_moe_lowers_and_codegens():
    n_exp = 2
    g = D.trace(
        moe_dense,
        spec(T, C),
        spec(C, n_exp),
        [spec(C, H)] * n_exp,
        [spec(H, C)] * n_exp,
    )
    sched = lower(g, VU13P_8CU)
    assert sum(b.engine.value == "matmul" for b in sched.bands) == 1 + 2 * n_exp
    prog = codegen(sched, T, C, C, VU13P_8CU)
    assert prog.insts
