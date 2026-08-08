"""Level-1 graph construction and the errors it must raise at build time.

The negative cases matter more than the positive ones: every one of them is a
mistake that would otherwise reach the hardware as a quietly wrong answer.
"""

import pytest

from ktpu.ir import (
    FP16,
    FP32,
    INT32,
    MXFP7,
    DTypeError,
    Graph,
    OpKind,
    ShapeError,
    broadcast,
)


def test_elementwise_broadcasts():
    g = Graph()
    a = g.input((4, 1, 8), FP16)
    b = g.input((3, 8), FP16)
    c = g.elementwise(OpKind.ADD, a, b)
    assert c.shape == (4, 3, 8)


def test_broadcast_rejects_incompatible():
    with pytest.raises(ShapeError):
        broadcast((3, 4), (5, 4))


def test_fp32_with_fp16_is_legal_on_the_vector_core():
    """E8 contains E5 and E8 IS FP32's exponent, so both convert on the load
    path with no range logic. Rejecting this would contradict why E8 was
    chosen."""
    g = Graph()
    a = g.input((8,), FP16)
    b = g.input((8,), FP32)
    assert g.elementwise(OpKind.MUL, a, b).dtype is FP32


def test_float_with_integer_is_still_an_error():
    """INT exists for indices and masks, not arithmetic."""
    g = Graph()
    with pytest.raises(DTypeError, match="mixed float and integer"):
        g.elementwise(OpKind.MUL, g.input((8,), FP16), g.input((8,), INT32))


def test_cast_still_forces_a_format():
    g = Graph()
    a = g.input((8,), FP32)
    assert g.cast(a, FP16).dtype is FP16


def test_matmul_is_m_k_by_k_n():
    """The M x K x N convention, pinned. Reading k off the wrong axis is how a
    GEMM gets silently transposed."""
    g = Graph()
    a = g.input((256, 1024), FP16)  # M x K
    b = g.input((1024, 512), FP16)  # K x N
    c = g.matmul(a, b)
    assert c.shape == (256, 512)
    assert g.producer(c).attrs["k"] == 1024


def test_matmul_mixes_prequantised_and_online_operands():
    """The ORDINARY inference case: weights uploaded once and pre-quantised,
    activations arriving per layer and quantised online in the read path. The
    legacy driver's preq=(bool, bool) says exactly this."""
    g = Graph()
    act = g.input((256, 1024), FP16)  # online quant, in mx_quant
    wgt = g.input((1024, 512), MXFP7)  # pre-quantised in DRAM
    c = g.matmul(act, wgt)
    assert c.shape == (256, 512)
    # MXFP7 decodes to an FP16-range value, so that is what the result holds.
    assert c.dtype is FP16


def test_matmul_inner_mismatch_names_both_shapes():
    g = Graph()
    a = g.input((256, 1024), FP16)
    b = g.input((512, 256), FP16)  # transposed by mistake
    with pytest.raises(ShapeError, match="inner dims disagree"):
        g.matmul(a, b)


def test_matmul_batches_broadcast():
    g = Graph()
    a = g.input((8, 1, 64, 32), FP16)
    b = g.input((4, 32, 16), FP16)
    assert g.matmul(a, b).shape == (8, 4, 64, 16)


def test_reduction_drops_or_keeps_the_axis():
    g = Graph()
    x = g.input((4, 8, 16), FP16)
    assert g.reduce(OpKind.SUM, x, (-1,)).shape == (4, 8)
    assert g.reduce(OpKind.SUM, x, (-1,), keepdim=True).shape == (4, 8, 1)
    assert g.reduce(OpKind.SUM, x, (0, 2)).shape == (8,)


def test_reduction_rejects_a_repeated_axis():
    g = Graph()
    x = g.input((4, 8), FP16)
    with pytest.raises(ShapeError, match="repeated axis"):
        g.reduce(OpKind.SUM, x, (0, -2))


def test_reshape_must_preserve_element_count():
    g = Graph()
    x = g.input((4, 8), FP16)
    assert g.reshape(x, (2, 16)).shape == (2, 16)
    with pytest.raises(ShapeError, match="element count"):
        g.reshape(x, (5, 7))


def test_permute_must_be_a_permutation():
    g = Graph()
    x = g.input((2, 3, 4), FP16)
    assert g.permute(x, (2, 0, 1)).shape == (4, 2, 3)
    with pytest.raises(ShapeError, match="not a permutation"):
        g.permute(x, (0, 0, 1))


def test_expand_only_grows_unit_axes():
    g = Graph()
    x = g.input((4, 1), FP16)
    assert g.expand(x, (4, 8)).shape == (4, 8)
    with pytest.raises(ShapeError, match="cannot expand"):
        g.expand(g.input((4, 2), FP16), (4, 8))


def test_arity_is_enforced():
    g = Graph()
    x = g.input((4,), FP16)
    with pytest.raises(ShapeError, match="takes 2 operands"):
        g.elementwise(OpKind.ADD, x)


def test_shapes_are_concrete_integers():
    """No symbolic extents: a pass that cannot read an extent cannot tile."""
    g = Graph()
    with pytest.raises(TypeError):
        g.input((4, None), FP16)
    with pytest.raises(ShapeError, match="negative extent"):
        g.input((4, -1), FP16)


def test_use_counts_find_dead_values():
    g = Graph()
    a = g.input((4,), FP16)
    live = g.elementwise(OpKind.NEG, a)
    dead = g.elementwise(OpKind.ABS, a)
    g.output(live)
    uses = g.uses()
    assert uses[live.vid] == 0 and live in g.outputs
    assert uses[dead.vid] == 0 and dead not in g.outputs


def test_graph_prints_readably():
    """A scheduling decision you cannot print is one you cannot review."""
    g = Graph("rmsnorm")
    x = g.input((256, 1024), FP16, name="x")
    ms = g.reduce(OpKind.SUMSQ, x, (-1,), keepdim=True)
    g.output(g.elementwise(OpKind.MUL, x, g.elementwise(OpKind.RSQRT, ms)))
    text = str(g)
    assert "graph rmsnorm" in text
    assert "sumsq" in text and "rsqrt" in text
    assert "return" in text
