"""One attention kernel, every configuration, executed at level 3.

`nn.attention` covers single head, multi head, GQA and causal through
compile-time flags. The reference is a float64 attention written the obvious
way -- build the whole score matrix, softmax it, multiply by V -- so it shares
no code with the kernel and a layout defect cannot cancel out.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen, cost_of
from ktpu.dsl.nn import attention
from ktpu.interp.mesh import Mesh
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as TGT

L, DH, BLOCK = 128, 64, 64
TRI = np.tril(np.ones((BLOCK, BLOCK)))


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def rand(rng, *shape):
    return rng.standard_normal(shape) * 0.3


def reference(q, k, v, causal=False):
    """Attention over a single `(L, D)` head, in float64."""
    s = (q @ k.T) * q.shape[-1] ** -0.5
    if causal:
        rows = np.arange(q.shape[0])[:, None]
        s = np.where(np.arange(k.shape[0])[None, :] <= rows, s, -np.inf)
    s = s - s.max(axis=-1, keepdims=True)
    p = np.exp(s)
    return (p / p.sum(axis=-1, keepdims=True)) @ v


def expected(q, k, v, causal=False, wo=None):
    """The reference laid out the way `attention` returns its pieces: batch,
    then query block, then head unless `wo` folded the heads away."""
    rank = q.ndim
    batch = q.shape[0] if rank == 4 else 1
    heads = q.shape[-3] if rank >= 3 else 1
    group = heads // (k.shape[-3] if rank >= 3 else 1)
    chan = q.shape[-1]

    def head(x, b, h):
        return x if rank == 2 else (x[h] if rank == 3 else x[b][h])

    pieces = []
    for b in range(batch):
        for i in range(L // BLOCK if causal else 1):
            joined, per_head = None, []
            for h in range(heads):
                full = reference(
                    head(q, b, h),
                    head(k, b, h // group),
                    head(v, b, h // group),
                    causal,
                )
                out = full[i * BLOCK : (i + 1) * BLOCK] if causal else full
                if wo is None:
                    per_head.append(out)
                else:
                    y = out @ wo[h * chan : (h + 1) * chan]
                    joined = y if joined is None else joined + y
            pieces += per_head if wo is None else [joined]
    return pieces


def execute(graph, arrays, m, k, n):
    sched = lower(graph, TGT)
    prog = codegen(sched, m, k, n, TGT)
    mesh = Mesh(prog, TGT)
    mesh.upload(sched.bands[0], arrays[0], arrays[1])
    names = [graph.producer(v).attrs["name"] for v in graph.inputs]
    for name, arr in list(zip(names, arrays, strict=True))[2:]:
        mesh.bind(name, arr)
    mesh.run()
    return [mesh.result(f"v{v.vid}") for v in graph.outputs], cost_of(prog, sched, TGT)


def build(shape, kv_shape, causal, use_wo, heads):
    """Trace one configuration; returns (graph, arrays, expected pieces)."""
    rng = np.random.default_rng(7)
    q = rand(rng, *shape)
    k, v = rand(rng, *kv_shape), rand(rng, *kv_shape)
    arrays, specs = [q, k, v], [spec(*shape), spec(*kv_shape), spec(*kv_shape)]
    wo = rand(rng, heads * DH, DH) if use_wo else None
    if causal:
        arrays.append(TRI)
        specs.append(spec(BLOCK, BLOCK))
    if use_wo:
        arrays.append(wo)
        specs.append(spec(heads * DH, DH))

    def kernel(qq, kk, vv, *rest):
        rest = list(rest)
        tri = rest.pop(0) if causal else None
        w = rest.pop(0) if use_wo else None
        return attention(qq, kk, vv, block=BLOCK, causal=causal, tri=tri, wo=w)

    return D.trace(kernel, *specs), arrays, expected(q, k, v, causal, wo)


CASES = [
    # (name, q shape, kv shape, causal, wo, heads)
    ("single head", (L, DH), (L, DH), False, False, 1),
    ("single head causal", (L, DH), (L, DH), True, False, 1),
    ("mha h=2", (2, L, DH), (2, L, DH), False, False, 2),
    ("mha h=2 + wo", (2, L, DH), (2, L, DH), False, True, 2),
    ("mha h=2 causal + wo", (2, L, DH), (2, L, DH), True, True, 2),
    ("gqa h=4 kv=2 + wo", (4, L, DH), (2, L, DH), False, True, 4),
    ("gqa h=4 kv=1 + wo", (4, L, DH), (1, L, DH), False, True, 4),
    ("gqa h=4 kv=2 causal", (4, L, DH), (2, L, DH), True, True, 4),
    ("batched b=2 h=2 + wo", (2, 2, L, DH), (2, 2, L, DH), False, True, 2),
    ("batched b=2 gqa kv=1", (2, 2, L, DH), (2, 1, L, DH), False, True, 2),
    ("batched causal + wo", (2, 2, L, DH), (2, 2, L, DH), True, True, 2),
]


@pytest.mark.parametrize(
    ("name", "qs", "kvs", "causal", "use_wo", "heads"),
    CASES,
    ids=[c[0].replace(" ", "_") for c in CASES],
)
def test_every_configuration_executes(name, qs, kvs, causal, use_wo, heads):
    graph, arrays, want = build(qs, kvs, causal, use_wo, heads)
    assert len(graph.outputs) == len(want), "one graph output per returned piece"
    rows = BLOCK if causal else L
    outs, _ = execute(graph, arrays, rows, DH, DH)
    for i, (got, ref) in enumerate(zip(outs, want, strict=True)):
        rel = np.abs(got - ref).max() / np.abs(ref).max()
        assert rel < 5e-3, f"{name} piece {i}: rel {rel:.2e}"


@pytest.mark.parametrize(("k1", "k2"), [(64, 64), (128, 64), (64, 128), (32, 64)])
def test_a_matmul_may_read_another_matmuls_output(k1, k2):
    """A DRAIN writes sub-tile order and a FILL reads L1-entry order, and only
    a vector store converts. Back-to-back GEMM used to return the right bytes
    in the wrong places -- 1.3 relative error, no exception -- so lower() now
    inserts a relayout band between them."""
    rng = np.random.default_rng(4)
    a = rand(rng, 64, k1)
    b = rng.standard_normal((k1, k2)) * 0.3
    c = rng.standard_normal((k2, 64)) * 0.3
    graph = D.trace(
        lambda x, y, z: (x @ y) @ z, spec(64, k1), spec(k1, k2), spec(k2, 64)
    )
    outs, _ = execute(graph, (a, b, c), 64, k2, 64)
    want = (a @ b) @ c
    assert np.abs(outs[0] - want).max() / np.abs(want).max() < 5e-3


def test_the_relayout_band_is_only_added_when_needed():
    """An elementwise op between the matmuls already converts, so inserting a
    second pass there would be pure overhead."""
    direct = lower(
        D.trace(lambda x, y, z: (x @ y) @ z, spec(64, 64), spec(64, 64), spec(64, 64)),
        TGT,
    )
    spaced = lower(
        D.trace(
            lambda x, y, z: D.gelu(x @ y) @ z, spec(64, 64), spec(64, 64), spec(64, 64)
        ),
        TGT,
    )
    assert any(b.name.startswith("relay") for b in direct.bands)
    assert not any(b.name.startswith("relay") for b in spaced.bands)


def _xfail(reason):
    return pytest.mark.xfail(
        reason=reason, strict=True, raises=(IndexError, ValueError)
    )


#: `(L, DH, BLOCK, causal)`. Head dim and block are INDEPENDENT; task #62 has
#: what still breaks. A block wider than the input is out of scope.
BLOCK_SIZES = [
    (128, 64, 64, False),
    (128, 64, 64, True),
    (64, 32, 32, False),
    (64, 32, 32, True),
    (128, 32, 64, False),
    (128, 64, 32, False),
    (128, 128, 64, False),
    (256, 128, 64, False),
    pytest.param(128, 32, 64, True, marks=_xfail("#62 causal with DH != BLOCK")),
    pytest.param(128, 64, 32, True, marks=_xfail("#62 causal with DH != BLOCK")),
    pytest.param(128, 128, 64, True, marks=_xfail("#62 causal with DH != BLOCK")),
    pytest.param(256, 32, 64, False, marks=_xfail("#62 DH below block, m > 1 tile")),
    pytest.param(256, 64, 128, False, marks=_xfail("#62 DH below block, m > 1 tile")),
]


@pytest.mark.parametrize(("L_", "dh", "block", "causal"), BLOCK_SIZES)
def test_block_sizes_are_independent(L_, dh, block, causal):
    rng = np.random.default_rng(3)
    q, k, v = (rng.standard_normal((L_, dh)) * 0.3 for _ in range(3))
    arrays, specs = [q, k, v], [spec(L_, dh)] * 3
    if causal:
        arrays.append(np.tril(np.ones((block, block))))
        specs.append(spec(block, block))
    graph = D.trace(
        lambda a, b, c, tri=None: attention(
            a, b, c, block=block, causal=causal, tri=tri
        ),
        *specs,
    )
    mm = [b for b in lower(graph, TGT).bands if b.engine.value == "matmul"]
    outs, _ = execute(graph, arrays, block if causal else L_, dh, dh)
    got = np.concatenate(outs)

    s = (q @ k.T) * dh**-0.5
    if causal:
        rows = np.arange(L_)[:, None]
        s = np.where(np.arange(L_)[None, :] <= rows, s, -np.inf)
    s = s - s.max(axis=-1, keepdims=True)
    p = np.exp(s)
    want = (p / p.sum(axis=-1, keepdims=True)) @ v
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-3
    if dh != block:
        assert len({b.ops[0].attrs["k"] for b in mm}) > 1, "two contraction widths"


def test_causal_skips_the_blocks_above_the_diagonal():
    """The saving is block matmuls not formed, not a cheaper mask: query block
    i forms i+1 of nb, so nb(nb+1)/2 of nb^2 -- 75% at nb=2."""
    dense, dense_arr, _ = build((L, DH), (L, DH), False, False, 1)
    causal, causal_arr, _ = build((L, DH), (L, DH), True, False, 1)
    _, dense_cost = execute(dense, dense_arr, L, DH, DH)
    _, causal_cost = execute(causal, causal_arr, BLOCK, DH, DH)
    nb = L // BLOCK
    assert causal_cost.flops == pytest.approx(
        dense_cost.flops * (nb + 1) / (2 * nb), rel=0.02
    )


def test_gqa_saves_kv_storage_and_not_flops():
    """Sharing K/V across query heads is a BANDWIDTH win. Every query head
    still runs its own attention, so the flop count must not move."""
    costs = {}
    for kv in (4, 2):
        graph, arrays, _ = build((4, L, DH), (kv, L, DH), False, True, 4)
        _, costs[kv] = execute(graph, arrays, L, DH, DH)
    assert costs[2].flops == costs[4].flops
    dropped = 2 * (2 * L * DH)  # two heads, of K and of V
    assert costs[4].image_words - costs[2].image_words == dropped // 2


def test_wo_collapses_the_head_axis_into_one_result():
    """Without wo each head is a separate piece, because joining them needs
    concat. With wo the sum over heads does the join, exactly."""
    plain, _, _ = build((4, L, DH), (4, L, DH), False, False, 4)
    fused, _, _ = build((4, L, DH), (4, L, DH), False, True, 4)
    assert len(plain.outputs) == 4
    assert len(fused.outputs) == 1


def test_an_option_that_is_off_is_not_in_the_graph():
    """Every flag is a compile-time if, so a plain call must not carry the
    ops the causal or multi-head paths would add."""
    single, _, _ = build((L, DH), (L, DH), False, False, 1)
    causal, _, _ = build((L, DH), (L, DH), True, False, 1)
    assert len(single.ops) < len(causal.ops)
    assert len(single.inputs) == 3, "no tri, no wo"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"causal": True}, "needs tri"),
        ({"block": 48}, "does not divide"),
    ],
)
def test_bad_configurations_say_what_is_wrong(kwargs, match):
    with pytest.raises(ValueError, match=match):
        D.trace(
            lambda a, b, c: attention(a, b, c, **kwargs),
            spec(L, DH),
            spec(L, DH),
            spec(L, DH),
        )


def test_a_kv_head_count_that_does_not_divide_is_refused():
    with pytest.raises(ValueError, match="does not divide"):
        D.trace(
            lambda a, b, c: attention(a, b, c, block=BLOCK),
            spec(4, L, DH),
            spec(3, L, DH),
            spec(3, L, DH),
        )


def test_rank_five_is_refused():
    with pytest.raises(ValueError, match="rank 2, 3 or 4"):
        D.trace(
            lambda a, b, c: attention(a, b, c, block=BLOCK),
            spec(2, 2, 2, L, DH),
            spec(2, 2, 2, L, DH),
            spec(2, 2, 2, L, DH),
        )


def test_the_untransposed_layout_is_the_one_that_does_not_lower():
    """(L, H*D) is what a QKV projection produces. A head of it is an inner
    multi-column slice, so it must be transposed to (H, L, D) first -- that is
    the previous layer's job, see docs/limits.md s6.3."""
    from ktpu.ir.sched import ScheduleError

    graph = D.trace(lambda x: x[:, 0:DH] * 2.0, spec(L, 2 * DH))
    with pytest.raises(ScheduleError, match="inner axis"):
        lower(graph, TGT)


def test_a_constant_read_against_a_drained_tile_is_packed_in_tile_order():
    """A DRAIN writes sub-tile order. An operand a vector core reads element
    for element against it must be STORED that way -- the read is sequential
    and nothing between memory and the ALU can permute."""
    rng = np.random.default_rng(2)
    a, b = rand(rng, BLOCK, BLOCK), rand(rng, BLOCK, BLOCK)
    graph = D.trace(
        lambda x, y, mask: (x @ y) * mask,
        spec(BLOCK, BLOCK),
        spec(BLOCK, BLOCK),
        spec(BLOCK, BLOCK),
    )
    prog = codegen(lower(graph, TGT), BLOCK, BLOCK, BLOCK, TGT)
    assert prog.packing[prog.inputs["mask"]]["role"] == "tile"
    outs, _ = execute(graph, (a, b, TRI), BLOCK, BLOCK, BLOCK)
    assert np.abs(outs[0] - (a @ b) * TRI).max() / np.abs((a @ b) * TRI).max() < 5e-3


def test_every_graph_output_gets_a_region():
    """A multi-result graph must not lose the results that are not last: the
    region is never allocated and the failure reads as a missing region."""
    graph = D.trace(lambda x: (x * 2.0, x * 3.0), spec(BLOCK, BLOCK))
    sched = lower(graph, TGT)
    assert sched.outputs == [v.vid for v in graph.outputs]
    prog = codegen(sched, BLOCK, 1, BLOCK, TGT)
    for v in graph.outputs:
        assert prog.memory.get(f"v{v.vid}") is not None
