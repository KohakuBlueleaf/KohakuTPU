"""Tracing an ordinary Python function into level-1 graph IR.

The guard tests matter more than every positive test here put together. A tracer
that answers `__bool__` instead of raising produces a graph that is right for the
input it happened to see and wrong for the next one, with nothing anywhere
recording that a choice was made -- and the whole claim of docs/driver/dsl.md is
that this frontend does not do that. Every other failure in this file is loud.
"""

import numpy as np
import pytest

from ktpu.dsl import (
    DSLError,
    TensorSpec,
    Tracer,
    TracerControlFlowError,
    TracerDTypeError,
    TracerShapeError,
    dot,
    exp,
    kernel,
    log,
    maximum,
    recip,
    relu,
    rsqrt,
    trace,
    where,
)
from ktpu.ir import FP16, FP32, INT32, MXFP7, OpKind, ShapeError

FOUR = TensorSpec((4,), FP16)


def kinds(graph):
    """The op kinds in order, sources dropped -- what the kernel actually did."""
    return [op.kind for op in graph.ops if op.kind not in (OpKind.INPUT, OpKind.CONST)]


def consts(graph):
    return [op.attrs["value"] for op in graph.ops if op.kind is OpKind.CONST]


# ---- the graph falls out of the operations ---------------------------------


def test_arithmetic_records_the_ops():
    graph = trace(lambda x, y: (x + y) * x, FOUR, FOUR)
    assert kinds(graph) == [OpKind.ADD, OpKind.MUL]
    assert graph.outputs[0].shape == (4,)


def test_a_helper_function_inlines_because_calling_it_is_calling_it():
    def scale(v):
        return v * 2.0

    graph = trace(lambda x: scale(scale(x)), FOUR)
    assert kinds(graph) == [OpKind.MUL, OpKind.MUL]


def test_a_python_loop_unrolls_because_it_runs():
    def repeated(x, *, times: int):
        for _ in range(times):
            x = x + x
        return x

    assert kinds(trace(repeated, FOUR, times=3)) == [OpKind.ADD] * 3


def test_a_comprehension_unrolls_too():
    def total(x):
        return sum([x * float(i) for i in range(1, 4)][1:], x)

    assert kinds(trace(total, FOUR)).count(OpKind.MUL) == 3


def test_compile_time_control_flow_is_ordinary_python():
    """`n_heads` is a plain int, so the branch is free and leaves no trace."""

    def head_split(x, *, n_heads: int):
        return x * x if n_heads > 8 else x + x

    assert kinds(trace(head_split, FOUR, n_heads=16)) == [OpKind.MUL]
    assert kinds(trace(head_split, FOUR, n_heads=4)) == [OpKind.ADD]


def test_shapes_are_plain_ints_at_trace_time():
    def uses_shape(x):
        return x * float(x.shape[-1] * 2)

    graph = trace(uses_shape, TensorSpec((8, 16), FP16))
    assert consts(graph) == [32.0]
    assert kinds(graph) == [OpKind.MUL]


# ---- the guards: runtime control flow is an error ---------------------------


def test_bool_raises_and_names_the_line_and_the_value():
    def branchy(x):
        if x:
            return x
        return -x

    with pytest.raises(TracerControlFlowError) as caught:
        trace(branchy, FOUR)
    message = str(caught.value)
    assert "bool()" in message
    assert "test_dsl.py" in message
    assert "if x:" in message, "the message must quote the guilty source line"
    assert "input 'x'" in message, "and say which value was data-dependent"
    assert "dsl.md" in message


def test_the_documented_failure_case():
    """`if x[0] > 0:` from docs/driver/dsl.md s2, verbatim."""

    def peeking(x):
        if x[0] > 0:
            return x
        return -x

    with pytest.raises(TracerControlFlowError, match="bool"):
        trace(peeking, FOUR)


@pytest.mark.parametrize(
    "attempt, construct",
    [
        (lambda x: len(x), "len()"),
        (lambda x: list(x), "iteration"),
        (lambda x: [1, 2, 3][x], "index"),
        (lambda x: float(x), "float()"),
        (lambda x: int(x), "int()"),
        (lambda x: x and x, "bool()"),
        (lambda x: not x, "bool()"),
        (lambda x: x in (1, 2), "iteration"),
    ],
)
def test_every_door_is_shut(attempt, construct):
    with pytest.raises(TracerControlFlowError) as caught:
        trace(attempt, FOUR)
    assert construct in str(caught.value)


def test_unpacking_raises_rather_than_yielding_two_tracers():
    def unpack(x):
        a, b = x
        return a + b

    with pytest.raises(TracerControlFlowError, match="iteration"):
        trace(unpack, TensorSpec((2,), FP16))


def test_no_branch_is_ever_silently_taken():
    """The failure mode this whole design exists to refuse.

    Tracing `x if x > 0 else -x` must not produce a graph at all -- not one for
    the positive case, not one for the negative. There is no graph that is right.
    """
    traced = []

    def sneaky(x):
        result = x if x > 0.0 else -x
        traced.append(result)
        return result

    with pytest.raises(TracerControlFlowError):
        trace(sneaky, FOUR)
    assert traced == []


def test_a_branch_becomes_a_value():
    """docs/driver/dsl.md s2's three replacements, in the order it gives them."""
    graph = trace(lambda x: where(x > 0.0, x, 0.0), FOUR)
    assert kinds(graph) == [OpKind.CMPGT, OpKind.SELECT]
    assert kinds(trace(lambda x: maximum(x, 0.0), FOUR)) == [OpKind.MAX]
    assert kinds(trace(relu, FOUR)) == [OpKind.RELU]


def test_indexing_with_a_traced_value_is_a_gather_and_is_refused():
    with pytest.raises(TracerControlFlowError, match="gather"):
        trace(lambda x, i: x[i], FOUR, FOUR)


# ---- numpy stays out -------------------------------------------------------


def test_a_numpy_array_does_not_broadcast_into_a_graph_of_object_ops():
    """Without __array_ufunc__ = None this silently yields an array of Tracers."""
    with pytest.raises(DSLError, match="ndarray"):
        trace(lambda x: np.array([1.0, 2.0, 3.0, 4.0]) * x, FOUR)


def test_numpy_cannot_materialise_a_tracer():
    with pytest.raises(DSLError):
        trace(lambda x: np.asarray(x), FOUR)


def test_a_numpy_scalar_is_a_constant_like_any_other():
    graph = trace(lambda x: x * np.float32(2.0), FOUR)
    assert kinds(graph) == [OpKind.MUL]
    assert consts(graph) == [2.0]


def test_a_numpy_array_argument_is_an_input():
    graph = trace(lambda x: x + x, np.zeros((3, 5), dtype=np.float16))
    assert graph.inputs[0].shape == (3, 5)
    assert graph.inputs[0].dtype is FP16


# ---- constants -------------------------------------------------------------


def test_a_repeated_scalar_is_one_constant():
    """Otherwise the op count depends on how the kernel was written."""

    def thrice(x):
        for _ in range(3):
            x = x * 2.0
        return x

    assert consts(trace(thrice, FOUR)) == [2.0]


def test_a_scalar_adopts_the_tensor_dtype():
    graph = trace(lambda x: x * 2.0, TensorSpec((4,), FP32))
    assert graph.ops[1].out.dtype is FP32


# ---- dtypes ----------------------------------------------------------------


def test_mixed_float_formats_converge_rather_than_raising():
    """The tracer must not be stricter than the IR (docs/driver/ir.md s1.1).

    `fp32 + fp16` is native to the vector core -- E8 contains E5 and E8 IS
    FP32's exponent -- so a frontend rule rejecting it would contradict the
    datapath the format was chosen for.
    """
    graph = trace(lambda a, b: a + b, FOUR, TensorSpec((4,), FP32))
    assert graph.outputs[0].dtype is FP32


def test_a_matmul_of_fp16_activations_by_mxfp7_weights_is_ordinary():
    """The inference case: weights pre-quantised once, activations quantised
    online in the read path. Nothing here should notice."""
    graph = trace(dot, TensorSpec((8, 16), FP16), TensorSpec((16, 4), MXFP7))
    assert kinds(graph) == [OpKind.MATMUL]
    assert graph.outputs[0].dtype is FP16, "MXFP7 decodes to FP16; converge says so"


def test_float_with_integer_raises_at_the_kernel_line():
    """INT is for indices and masks, not arithmetic -- the one mix still refused."""
    with pytest.raises(TracerDTypeError) as caught:
        trace(lambda a, b: a + b, FOUR, TensorSpec((4,), INT32))
    assert "test_dsl.py" in str(caught.value)


def test_cast_is_a_method_and_a_function():
    graph = trace(lambda x: x.to(FP32), FOUR)
    assert kinds(graph) == [OpKind.CAST]
    assert graph.outputs[0].dtype is FP32


# ---- shape errors, at the line that caused them ----------------------------


def test_a_shape_error_names_the_kernel_line():
    def mismatched(a, b):
        return a + b

    with pytest.raises(TracerShapeError) as caught:
        trace(mismatched, TensorSpec((3, 4), FP16), TensorSpec((5, 4), FP16))
    message = str(caught.value)
    assert "cannot broadcast" in message
    assert "return a + b" in message


def test_a_tracer_shape_error_is_still_a_shape_error():
    """Callers that catch the IR's error keep working."""
    with pytest.raises(ShapeError):
        trace(
            lambda a, b: dot(a, b), TensorSpec((4, 8), FP16), TensorSpec((4, 8), FP16)
        )


# ---- reductions ------------------------------------------------------------


def test_reductions_keep_or_drop_the_axis():
    spec = TensorSpec((4, 8, 16), FP16)
    assert trace(lambda x: x.sum(-1), spec).outputs[0].shape == (4, 8)
    assert trace(lambda x: x.sum(-1, keepdim=True), spec).outputs[0].shape == (4, 8, 1)
    assert trace(lambda x: x.sum((0, 2)), spec).outputs[0].shape == (8,)
    assert trace(lambda x: x.sum(), spec).outputs[0].shape == ()


def test_max_min_and_sumsq_are_single_ops():
    spec = TensorSpec((4, 8), FP16)
    assert kinds(trace(lambda x: x.max(-1), spec)) == [OpKind.RMAX]
    assert kinds(trace(lambda x: x.min(-1), spec)) == [OpKind.RMIN]
    assert kinds(trace(lambda x: x.sumsq(-1), spec)) == [OpKind.SUMSQ]


def test_mean_is_sugar_that_leaves_no_trace_of_itself():
    graph = trace(lambda x: x.mean(-1), TensorSpec((4, 8), FP16))
    assert kinds(graph) == [OpKind.SUM, OpKind.MUL]
    assert consts(graph) == [1.0 / 8]


def test_var_is_the_two_pass_form():
    """`E[x^2] - E[x]^2` is one pass cheaper and cancels in 16 mantissa bits."""
    graph = trace(lambda x: x.var(-1), TensorSpec((4, 8), FP16))
    assert kinds(graph) == [
        OpKind.SUM,
        OpKind.MUL,  # the mean
        OpKind.SUB,  # the deviation
        OpKind.MUL,  # squared
        OpKind.SUM,
        OpKind.MUL,  # and its mean
    ]


# ---- views -----------------------------------------------------------------


def test_views_are_ops_with_shapes():
    spec = TensorSpec((2, 3, 4), FP16)
    assert trace(lambda x: x.reshape(6, 4), spec).outputs[0].shape == (6, 4)
    assert trace(lambda x: x.permute(2, 0, 1), spec).outputs[0].shape == (4, 2, 3)
    assert trace(lambda x: x.transpose(), spec).outputs[0].shape == (2, 4, 3)
    assert trace(lambda x: x.pad(((0, 0), (1, 1), (0, 2))), spec).outputs[0].shape == (
        2,
        5,
        6,
    )


def test_expand_grows_unit_axes_and_broadcast_to_may_add_them():
    spec = TensorSpec((4, 1), FP16)
    assert trace(lambda x: x.expand(4, 8), spec).outputs[0].shape == (4, 8)
    assert trace(lambda x: x.broadcast_to(2, 4, 8), spec).outputs[0].shape == (2, 4, 8)


def test_slicing_produces_a_slice_op():
    graph = trace(lambda x: x[:, 2:6], TensorSpec((4, 8), FP16))
    assert kinds(graph) == [OpKind.SLICE]
    assert graph.outputs[0].shape == (4, 4)
    assert graph.producer(graph.outputs[0]).attrs == {"begin": (0, 2), "end": (4, 6)}


def test_an_integer_index_drops_the_axis():
    graph = trace(lambda x: x[0], TensorSpec((4, 8), FP16))
    assert kinds(graph) == [OpKind.SLICE, OpKind.RESHAPE]
    assert graph.outputs[0].shape == (8,)


def test_a_full_slice_emits_nothing():
    assert kinds(trace(lambda x: x[:] + x, TensorSpec((4,), FP16))) == [OpKind.ADD]


def test_a_strided_slice_is_refused_because_it_is_a_gather():
    with pytest.raises(DSLError, match="strided"):
        trace(lambda x: x[::2], TensorSpec((4,), FP16))


def test_an_out_of_range_index_is_caught_at_trace_time():
    with pytest.raises(DSLError, match="out of range"):
        trace(lambda x: x[9], TensorSpec((4,), FP16))


# ---- contraction -----------------------------------------------------------


def test_dot_is_m_k_by_k_n():
    graph = trace(dot, TensorSpec((256, 1024), FP16), TensorSpec((1024, 512), FP16))
    assert kinds(graph) == [OpKind.MATMUL]
    assert graph.outputs[0].shape == (256, 512)
    assert graph.producer(graph.outputs[0]).attrs["k"] == 1024


def test_the_matmul_operator_is_the_same_op():
    graph = trace(
        lambda a, b: a @ b, TensorSpec((4, 8), FP16), TensorSpec((8, 2), FP16)
    )
    assert kinds(graph) == [OpKind.MATMUL]


# ---- comparisons and the one-comparison op set -----------------------------


def test_the_three_hardware_comparisons_are_one_op_each():
    """VCMPLT, VCMPGT and VCMPEQ exist, so `<`, `>` and `==` must not compose."""
    assert kinds(trace(lambda a, b: a < b, FOUR, FOUR)) == [OpKind.CMPLT]
    assert kinds(trace(lambda a, b: a > b, FOUR, FOUR)) == [OpKind.CMPGT]
    assert kinds(trace(lambda a, b: a == b, FOUR, FOUR)) == [OpKind.CMPEQ]


def test_greater_than_does_not_flip_its_operands():
    """`a > b` is CMPGT(a, b), not CMPLT(b, a); flipping loses argument order."""
    graph = trace(lambda a, b: a > b, FOUR, FOUR)
    assert graph.ops[-1].inputs == (graph.inputs[0], graph.inputs[1])


def test_the_negated_comparisons_cost_one_extra_op():
    """A mask is 1.0 or 0.0, so `1 - m` inverts it."""
    for fn, base in (
        (lambda a, b: a <= b, OpKind.CMPGT),
        (lambda a, b: a >= b, OpKind.CMPLT),
        (lambda a, b: a != b, OpKind.CMPEQ),
    ):
        assert kinds(trace(fn, FOUR, FOUR)) == [base, OpKind.SUB]


def test_equality_is_elementwise_but_a_tracer_is_still_hashable():
    """`__eq__` returns a mask, so identity is the only honest hash left."""
    seen = []

    def probe(a, b):
        seen.append(isinstance(a == b, Tracer))
        seen.append(len({a, b, a}) == 2)
        return a + b

    trace(probe, FOUR, FOUR)
    assert seen == [True, True]


# ---- expansions ------------------------------------------------------------


def test_exp_and_log_are_base_two_with_the_constant_folded():
    assert kinds(trace(exp, FOUR)) == [OpKind.MUL, OpKind.EXP2]
    assert kinds(trace(log, FOUR)) == [OpKind.LOG2, OpKind.MUL]


def test_one_over_x_is_recip_not_a_divide():
    assert kinds(trace(lambda x: 1.0 / x, FOUR)) == [OpKind.RECIP]
    assert kinds(trace(recip, FOUR)) == [OpKind.RECIP]
    assert kinds(trace(lambda x: 2.0 / x, FOUR)) == [OpKind.DIV]


@pytest.mark.parametrize(
    "power, expected",
    [
        (lambda x: x**2, [OpKind.MUL]),
        (lambda x: x**3, [OpKind.MUL, OpKind.MUL]),
        (lambda x: x**-1, [OpKind.RECIP]),
        (lambda x: x**0.5, [OpKind.SQRT]),
        (lambda x: x**-0.5, [OpKind.RSQRT]),
        (rsqrt, [OpKind.RSQRT]),
        # No `pow` op exists or is wanted: anything else is 2^(n log2 x), and
        # both halves are single-pass on the vector core.
        (lambda x: x**1.5, [OpKind.LOG2, OpKind.MUL, OpKind.EXP2]),
        (lambda x: 2.0**x, [OpKind.EXP2]),
    ],
)
def test_powers_expand_at_trace_time(power, expected):
    assert kinds(trace(power, FOUR)) == expected


def test_abs_and_neg_are_the_builtins():
    assert kinds(trace(lambda x: abs(-x), FOUR)) == [OpKind.NEG, OpKind.ABS]


def test_builtin_max_is_a_branch_and_says_so():
    with pytest.raises(TracerControlFlowError, match="bool"):
        trace(lambda a, b: max(a, b), FOUR, FOUR)


# ---- entry points and the cache --------------------------------------------


def test_the_same_arguments_give_the_same_graph_object():
    @kernel
    def double(x):
        return x + x

    first = double(FOUR)
    assert double(FOUR) is first
    assert double.cache_info() == {"hits": 1, "misses": 1, "size": 1}
    assert "double" in repr(double)


def test_a_different_shape_or_dtype_is_a_different_graph():
    @kernel
    def double(x):
        return x + x

    assert double(FOUR) is not double(TensorSpec((8,), FP16))
    assert double(FOUR) is not double(TensorSpec((4,), FP32))
    assert double.cache_info()["size"] == 3


def test_changing_only_a_scalar_changes_the_graph():
    """docs/driver/dsl.md s5 asks for this one by name."""

    @kernel
    def scaled(x, *, alpha: float):
        return x * alpha

    half = scaled(FOUR, alpha=0.5)
    third = scaled(FOUR, alpha=1.0 / 3)
    assert half is not third
    assert consts(half) == [0.5] and consts(third) == [1.0 / 3]


def test_a_scalar_that_steers_a_branch_changes_the_graph_structurally():
    @kernel
    def head_split(x, *, n_heads: int):
        return x * x if n_heads > 8 else x + x

    assert kinds(head_split(FOUR, n_heads=16)) == [OpKind.MUL]
    assert kinds(head_split(FOUR, n_heads=4)) == [OpKind.ADD]


def test_an_int_and_a_float_are_not_the_same_key():
    """hash(1) == hash(1.0) == hash(True), and all three compare equal."""

    @kernel
    def scaled(x, *, alpha):
        return x * alpha

    assert scaled(FOUR, alpha=1) is not scaled(FOUR, alpha=1.0)


def test_positional_and_keyword_reach_the_same_entry():
    @kernel
    def scaled(x, alpha=2.0):
        return x * alpha

    assert scaled(FOUR, 2.0) is scaled(FOUR, alpha=2.0) is scaled(FOUR)


def test_an_unhashable_argument_is_refused_rather_than_mis_keyed():
    @kernel
    def configured(x, *, opts):
        return x * opts.pop()

    with pytest.raises(DSLError, match="hashable"):
        configured(FOUR, opts={2.0})


def test_trace_does_not_touch_the_cache():
    @kernel
    def double(x):
        return x + x

    assert double.retrace(FOUR) is not double.retrace(FOUR)
    assert double.cache_info()["size"] == 0


def test_a_kernel_returns_a_tuple_for_several_outputs():
    graph = trace(lambda x: (x + x, x * x), FOUR)
    assert len(graph.outputs) == 2
    assert kinds(graph) == [OpKind.ADD, OpKind.MUL]


def test_a_kernel_that_returns_a_plain_value_has_no_graph():
    with pytest.raises(DSLError, match="traced values"):
        trace(lambda x: 3.0, FOUR)


def test_tensors_nested_in_containers_become_inputs():
    graph = trace(lambda ws: ws[0] + ws[1], [FOUR, FOUR])
    assert len(graph.inputs) == 2
    assert graph.producer(graph.inputs[0]).attrs["name"] == "ws[0]"


def test_a_kernel_cannot_be_called_with_a_traced_value():
    @kernel
    def inner(x):
        return x + x

    with pytest.raises(DSLError, match="undecorated"):
        trace(lambda x: inner(x), FOUR)


# ---- escaping a trace ------------------------------------------------------


def test_a_tracer_belongs_to_the_one_call_that_created_it():
    """Stashing one in a global would otherwise append to a finished graph."""
    escaped = []
    trace(lambda x: escaped.append(x) or x, FOUR)
    with pytest.raises(DSLError, match="escaped"):
        escaped[0] + escaped[0]
    with pytest.raises(DSLError, match="different traces"):
        trace(lambda x: x + escaped[0], FOUR)


def test_a_tracer_prints_its_shape_dtype_and_origin():
    seen = []
    trace(lambda x: seen.append(repr(x * x)) or x, FOUR)
    assert "4:fp16" in seen[0] and "from mul" in seen[0]


def test_the_public_surface_is_importable_by_name():
    """A DSL nobody can `from ktpu.dsl import *` is a DSL nobody will use."""
    from ktpu import dsl

    for name in dsl.__all__:
        assert hasattr(dsl, name), name
    assert isinstance(dsl.Tracer, type) and Tracer is dsl.Tracer
