"""The kernel library, traced and then executed against numpy.

docs/driver/dsl.md s6 step 5 calls this the honest test of the whole design: if
`rmsnorm`, `softmax`, `silu` or `gelu` cannot be written in the DSL without an
escape hatch, the surface is wrong. None of them needs one -- every kernel in
`ktpu.dsl.library` is ordinary Python arithmetic on tracers.

Asserting the op list alone would be a weak test: it says the graph has the shape
someone expected, not that it computes the function. So the graph is interpreted
here in float64 and compared against the definition written out directly. The
interpreter is deliberately naive and lives only in this file -- it is a
reference for the frontend, not a numeric model of the machine (that is
`vec_model.py`, docs/compute/vector-bringup.md s4.1).
"""

import inspect
import math

import numpy as np
import pytest

from ktpu.dsl import TensorSpec, trace
from ktpu.dsl.library import (
    gelu,
    layernorm,
    rmsnorm,
    sigmoid,
    silu,
    softmax,
    split_k_epilogue,
    tanh,
)
from ktpu.ir import ACC24, FP16, FP32, OpKind

RNG = np.random.default_rng(20260808)


def evaluate(graph, **feeds) -> list[np.ndarray]:
    """Run a level-1 graph in float64. Inputs are named by their kernel parameter."""
    env: dict[int, np.ndarray] = {}
    for op in graph.ops:
        env[op.out.vid] = _apply(op, [env[v.vid] for v in op.inputs], feeds)
    return [env[v.vid] for v in graph.outputs]


def _apply(op, xs, feeds):
    attrs = op.attrs
    match op.kind:
        case OpKind.INPUT:
            return np.asarray(feeds[attrs["name"]], dtype=np.float64)
        case OpKind.CONST:
            return np.float64(attrs["value"])
        case OpKind.NEG:
            return -xs[0]
        case OpKind.ABS:
            return np.abs(xs[0])
        case OpKind.RECIP:
            return 1.0 / xs[0]
        case OpKind.RSQRT:
            return 1.0 / np.sqrt(xs[0])
        case OpKind.SQRT:
            return np.sqrt(xs[0])
        case OpKind.EXP2:
            return np.exp2(xs[0])
        case OpKind.LOG2:
            return np.log2(xs[0])
        case OpKind.RELU:
            return np.maximum(xs[0], 0.0)
        case OpKind.ADD:
            return xs[0] + xs[1]
        case OpKind.SUB:
            return xs[0] - xs[1]
        case OpKind.MUL:
            return xs[0] * xs[1]
        case OpKind.DIV:
            return xs[0] / xs[1]
        case OpKind.MAX:
            return np.maximum(xs[0], xs[1])
        case OpKind.MIN:
            return np.minimum(xs[0], xs[1])
        # A mask is 1.0 or 0.0 in the operand dtype -- there is no bool in the
        # machine, and `select` consumes it as a value.
        case OpKind.CMPLT:
            return (xs[0] < xs[1]).astype(np.float64)
        case OpKind.FMA:
            return xs[0] * xs[1] + xs[2]
        case OpKind.SELECT:
            return np.where(xs[0] != 0.0, xs[1], xs[2])
        case OpKind.SUM:
            return xs[0].sum(axis=attrs["axes"], keepdims=attrs["keepdim"])
        case OpKind.SUMSQ:
            return (xs[0] ** 2).sum(axis=attrs["axes"], keepdims=attrs["keepdim"])
        case OpKind.RMAX:
            return xs[0].max(axis=attrs["axes"], keepdims=attrs["keepdim"])
        case OpKind.RMIN:
            return xs[0].min(axis=attrs["axes"], keepdims=attrs["keepdim"])
        case OpKind.MATMUL:
            return xs[0] @ xs[1]
        case OpKind.RESHAPE:
            return xs[0].reshape(op.out.shape)
        case OpKind.PERMUTE:
            return xs[0].transpose(attrs["order"])
        case OpKind.EXPAND:
            return np.broadcast_to(xs[0], op.out.shape)
        case OpKind.SLICE:
            cut = tuple(slice(b, e) for b, e in zip(attrs["begin"], attrs["end"]))
            return xs[0][cut]
        case OpKind.PAD:
            return np.pad(xs[0], attrs["pads"], constant_values=attrs["value"])
        # Precision is not what this file checks, so a cast is the identity here.
        # What the real conversions cost is `ir/dtype.py`'s business.
        case OpKind.CAST:
            return xs[0]
        case _:
            raise AssertionError(f"the interpreter has no case for {op.kind}")


def op_kinds(graph):
    return [op.kind for op in graph.ops]


def spec(shape, dtype=FP32):
    return TensorSpec(shape, dtype)


# ---- the graphs are straight-line level-1 IR -------------------------------


@pytest.mark.parametrize(
    "fn, args",
    [
        (rmsnorm, (spec((4, 32)), spec((32,)))),
        (layernorm, (spec((4, 32)), spec((32,)), spec((32,)))),
        (softmax, (spec((4, 32)),)),
        (silu, (spec((4, 32)),)),
        (gelu, (spec((4, 32)),)),
        (sigmoid, (spec((4, 32)),)),
        (tanh, (spec((4, 32)),)),
    ],
)
def test_every_kernel_traces_to_ops_in_dependency_order(fn, args):
    """No forward references, so a pass can walk the list once."""
    graph = trace(fn, *args)
    produced: set[int] = set()
    for op in graph.ops:
        assert all(v.vid in produced for v in op.inputs), f"{op} reads ahead"
        produced.add(op.out.vid)
    assert graph.outputs and all(v.vid in produced for v in graph.outputs)


@pytest.mark.parametrize("fn", [rmsnorm, layernorm, softmax, silu, gelu])
def test_no_kernel_becomes_a_node_of_its_own_name(fn):
    """`softmax` and `gelu` are compositions; a node would hide the fusion."""
    graph = trace(fn, *[spec((4, 32))] * _n_required(fn))
    names = {op.kind.value for op in graph.ops}
    assert fn.__name__ not in names
    assert len(names - {"input", "const"}) > 1, "it decomposed into real work"


def _n_required(fn) -> int:
    return sum(
        p.default is inspect.Parameter.empty
        for p in inspect.signature(fn).parameters.values()
    )


# ---- and they compute what they say ----------------------------------------


def test_rmsnorm_matches_its_definition():
    x = RNG.standard_normal((8, 64))
    g = RNG.standard_normal(64)
    eps = 1e-6

    graph = trace(rmsnorm, spec((8, 64)), spec((64,)), eps)
    (got,) = evaluate(graph, x=x, g=g)

    want = x / np.sqrt((x**2).mean(axis=-1, keepdims=True) + eps) * g
    np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-14)


def test_rmsnorm_has_exactly_one_reduction():
    """What makes it one fused pass at level 2, and the reason to check."""
    graph = trace(rmsnorm, spec((8, 64)), spec((64,)))
    assert op_kinds(graph).count(OpKind.SUM) == 1
    assert not {OpKind.RMAX, OpKind.RMIN, OpKind.SUMSQ} & set(op_kinds(graph))


def test_softmax_matches_its_definition_and_sums_to_one():
    x = RNG.standard_normal((8, 64)) * 10.0

    graph = trace(softmax, spec((8, 64)))
    (got,) = evaluate(graph, x=x)

    shifted = np.exp(x - x.max(axis=-1, keepdims=True))
    want = shifted / shifted.sum(axis=-1, keepdims=True)
    np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-14)
    np.testing.assert_allclose(got.sum(axis=-1), 1.0, rtol=1e-12)


def test_softmax_subtracts_the_max_before_exponentiating():
    """Not optional: exp2 overflows E8M15 at x = 88, which real logits reach."""
    graph = trace(softmax, spec((8, 64)))
    kinds = op_kinds(graph)
    assert kinds.index(OpKind.RMAX) < kinds.index(OpKind.EXP2)

    huge = np.full((2, 4), 300.0)
    huge[:, 0] = 400.0
    (got,) = evaluate(graph, x=huge)
    assert np.isfinite(got).all()
    np.testing.assert_allclose(got[:, 0], 1.0, atol=1e-12)


def test_sigmoid_and_tanh_match_numpy():
    x = RNG.standard_normal((4, 16)) * 3.0
    (got,) = evaluate(trace(sigmoid, spec((4, 16))), x=x)
    np.testing.assert_allclose(got, 1.0 / (1.0 + np.exp(-x)), rtol=1e-12)
    (got,) = evaluate(trace(tanh, spec((4, 16))), x=x)
    np.testing.assert_allclose(got, np.tanh(x), rtol=1e-11, atol=1e-14)


def test_tanh_saturates_rather_than_producing_nan():
    """The +/-1 limits come out of the algebra with no guard, and must.

    The overflow is the mechanism, not a bug: exp2 goes to infinity, recip of it
    to zero, and the expression to exactly +1.
    """
    graph = trace(tanh, spec((4,)))
    with np.errstate(over="ignore"):
        (got,) = evaluate(graph, x=np.array([-500.0, -40.0, 40.0, 500.0]))
    np.testing.assert_allclose(got, [-1.0, -1.0, 1.0, 1.0], atol=1e-12)


def test_silu_matches_its_definition():
    x = RNG.standard_normal((4, 16)) * 3.0
    (got,) = evaluate(trace(silu, spec((4, 16))), x=x)
    np.testing.assert_allclose(got, x / (1.0 + np.exp(-x)), rtol=1e-12)


def test_gelu_matches_the_tanh_approximation_and_tracks_the_exact_one():
    x = RNG.standard_normal((4, 16)) * 2.0

    (got,) = evaluate(trace(gelu, spec((4, 16))), x=x)
    inner = np.sqrt(2.0 / np.pi) * (x + 0.044715 * x**3)
    np.testing.assert_allclose(got, 0.5 * x * (1.0 + np.tanh(inner)), rtol=1e-10)

    # The approximation is the point of the approximation: it has to be close to
    # the erf form, or naming it `gelu` is a lie.
    exact = 0.5 * x * (1.0 + np.vectorize(math.erf)(x / np.sqrt(2.0)))
    np.testing.assert_allclose(got, exact, atol=2e-3)


def test_gelu_picks_its_expansion_at_trace_time():
    """`approximate` is a plain string, so only one branch reaches the graph."""
    by_tanh = trace(gelu, spec((4,)), "tanh")
    by_sigmoid = trace(gelu, spec((4,)), "sigmoid")
    assert len(by_sigmoid.ops) < len(by_tanh.ops)
    x = RNG.standard_normal(4)
    (a,) = evaluate(by_tanh, x=x)
    (b,) = evaluate(by_sigmoid, x=x)
    np.testing.assert_allclose(a, b, atol=2e-2)


def test_layernorm_matches_its_definition():
    x = RNG.standard_normal((8, 64)) + 5.0
    g, b = RNG.standard_normal(64), RNG.standard_normal(64)
    eps = 1e-5

    graph = trace(layernorm, spec((8, 64)), spec((64,)), spec((64,)), eps)
    (got,) = evaluate(graph, x=x, g=g, b=b)

    centred = x - x.mean(axis=-1, keepdims=True)
    want = centred / np.sqrt(centred.var(axis=-1, keepdims=True) + eps) * g + b
    np.testing.assert_allclose(got, want, rtol=1e-11, atol=1e-13)


def test_layernorm_takes_its_mean_once():
    graph = trace(layernorm, spec((8, 64)), spec((64,)), spec((64,)))
    assert op_kinds(graph).count(OpKind.SUM) == 2


# ---- the split-K epilogue --------------------------------------------------


def test_split_k_epilogue_sums_the_partials_and_converts_once():
    graph = trace(split_k_epilogue, TensorSpec((4, 16, 32), ACC24))

    casts = [op for op in graph.ops if op.kind is OpKind.CAST]
    assert [op.out.dtype.name for op in casts] == ["e8m15", "fp16"], (
        "ACC24 -> E8M15 before the sum so it carries E8's range, and exactly one "
        "conversion after it -- docs/compute/vector-core.md s11.3"
    )
    assert op_kinds(graph).count(OpKind.SUM) == 1
    assert graph.outputs[0].shape == (16, 32)

    partials = RNG.standard_normal((4, 16, 32))
    (got,) = evaluate(graph, partials=partials)
    np.testing.assert_allclose(got, partials.sum(axis=0), rtol=1e-12)


def test_split_k_epilogue_can_keep_fp32_when_the_range_needs_it():
    graph = trace(split_k_epilogue, TensorSpec((4, 16, 32), ACC24), FP32)
    assert graph.outputs[0].dtype is FP32


# ---- a whole layer, to show the pieces compose -----------------------------


def test_an_ffn_block_traces_end_to_end():
    """rmsnorm, two matmuls and an activation -- one graph, no escape hatch."""

    def ffn(x, norm_g, w_up, w_gate, w_down, *, eps: float = 1e-6):
        h = rmsnorm(x, norm_g, eps)
        return silu(h @ w_gate) * (h @ w_up) @ w_down

    graph = trace(
        ffn,
        spec((8, 64)),
        spec((64,)),
        spec((64, 128)),
        spec((64, 128)),
        spec((128, 64)),
    )
    assert op_kinds(graph).count(OpKind.MATMUL) == 3
    assert graph.outputs[0].shape == (8, 64)

    feeds = {
        "x": RNG.standard_normal((8, 64)),
        "norm_g": RNG.standard_normal(64),
        "w_up": RNG.standard_normal((64, 128)) * 0.1,
        "w_gate": RNG.standard_normal((64, 128)) * 0.1,
        "w_down": RNG.standard_normal((128, 64)) * 0.1,
    }
    (got,) = evaluate(graph, **feeds)

    x, g = feeds["x"], feeds["norm_g"]
    h = x / np.sqrt((x**2).mean(axis=-1, keepdims=True) + 1e-6) * g
    gate = h @ feeds["w_gate"]
    want = (gate / (1.0 + np.exp(-gate)) * (h @ feeds["w_up"])) @ feeds["w_down"]
    np.testing.assert_allclose(got, want, rtol=1e-10, atol=1e-12)


def test_the_ffn_is_specialised_per_shape_and_nothing_symbolic_reaches_the_ir():
    """Dynamic shape is this layer's job and stops here (docs/driver/ir.md s6.1)."""
    for seq in (8, 16, 37):
        graph = trace(rmsnorm, spec((seq, 64)), spec((64,)))
        assert all(
            isinstance(d, int) for op in graph.ops for d in op.out.shape
        ), "every extent is a concrete integer"
        assert graph.outputs[0].shape == (seq, 64)


def test_a_kernel_can_be_traced_against_fp16_specs_without_touching_a_buffer():
    graph = trace(rmsnorm, TensorSpec((256, 1024), FP16), TensorSpec((1024,), FP16))
    assert graph.outputs[0].dtype is FP16
    assert graph.outputs[0].shape == (256, 1024)
