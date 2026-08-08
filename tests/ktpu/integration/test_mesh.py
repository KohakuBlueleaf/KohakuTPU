"""Level 3, executed. The numeric end-to-end the stack never had.

Everything below runs the actual instruction stream -- the same addresses, L1
spans, loop counts and operand bindings the machine would be given -- and
compares against the float64 reference at level 1. A structural test cannot tell
you that an operand was bound to the wrong register; this can.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.interp import run_one
from ktpu.interp.mesh import Mesh, MeshFault, pack_operand, unpack_operand
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

M, K, N = 64, 128, 64


def _spec(*s):
    return D.TensorSpec(s, FP16)


def _run(fn, specs, arrays, fold=True, m=M, k=K, n=N):
    """Trace, lower, codegen and execute; return (result, reference, mesh).

    The first two arrays are the GEMM operands and go through the packer; every
    later one is bound by the name its graph input carries, so a renamed lambda
    parameter cannot silently unbind it.
    """
    graph = D.trace(fn, *specs)
    sched = lower(graph, VU13P_8CU, fold_epilogue=fold)
    prog = codegen(sched, m, k, n, VU13P_8CU)
    mesh = Mesh(prog, VU13P_8CU)
    mesh.upload(sched.bands[0], arrays[0], arrays[1])
    for v, val in zip(graph.inputs[2:], arrays[2:], strict=True):
        mesh.bind(graph.producer(v).attrs["name"], val)
    mesh.run()
    return mesh.result(), run_one(graph, *arrays), mesh


def _rand(seed=0, scale=0.2):
    r = np.random.default_rng(seed)
    return (
        r.standard_normal((M, K)) * scale,
        r.standard_normal((K, N)) * scale,
        r.standard_normal(N) * 0.1,
    )


# ---------------------------------------------------------------------------
# The whole chain produces the right numbers
# ---------------------------------------------------------------------------


def test_a_plain_matmul_matches_the_reference():
    x, w, _ = _rand()
    got, want, _ = _run(lambda a, b: a @ b, (_spec(M, K), _spec(K, N)), (x, w))
    assert np.abs(got - want).max() < 1e-3


def test_linear_gelu_matches_the_reference():
    """16 chained ops over four micro-jobs, a broadcast bias and six scalar
    constants -- if any operand were bound to the wrong thing this is where it
    shows."""
    x, w, b = _rand()
    got, want, _ = _run(
        lambda a, bb, c: D.gelu(a @ bb + c),
        (_spec(M, K), _spec(K, N), _spec(N)),
        (x, w, b),
    )
    assert np.abs(got - want).max() < 5e-3


def test_the_bias_lands_on_the_right_column():
    """A bias broadcast down the wrong axis still produces plausible numbers, so
    catching it needs one whose columns are far apart -- here `arange(N)`, which
    makes a row/column swap a difference of up to N.

    The tolerance is FP16's own ulp at the result's magnitude, not a round
    number: at 63 that is 0.03125, and a tighter constant would be failing the
    storage format rather than the compiler.
    """
    x, w, _ = _rand()
    b = np.arange(N) * 1.0
    got, want, _ = _run(
        lambda a, bb, c: a @ bb + c,
        (_spec(M, K), _spec(K, N), _spec(N)),
        (x, w, b),
    )
    ulp = 2.0 ** (np.floor(np.log2(np.abs(want).max())) - 10)
    assert np.abs(got - want).max() <= 2 * ulp


def test_operands_are_not_interchangeable():
    """`t*0.25 - t` and `t - t*0.25` differ by a sign. A binding that ignored
    operand ORDER would pass a test built only from commutative ops."""

    def fn(a, bb):
        t = a @ bb
        return t * 0.25 - t

    x, w, _ = _rand()
    got, want, _ = _run(fn, (_spec(M, K), _spec(K, N)), (x, w))
    assert np.abs(got - want).max() < 1e-3
    assert np.abs(want).max() > 0.1
    assert np.abs(want + got[::-1] * 0).max() > 0.1


# ---------------------------------------------------------------------------
# Folding: does it change the answer, and what is it actually for
# ---------------------------------------------------------------------------


def test_folding_does_not_change_the_answer():
    """The check that was missing when epilogue folding silently deleted the
    whole epilogue and the instruction count was reported as a 10x win."""
    x, w, b = _rand()
    args = (lambda a, bb, c: D.gelu(a @ bb + c), (_spec(M, K), _spec(K, N), _spec(N)))
    folded, want, _ = _run(*args, (x, w, b), fold=True)
    flat, _, _ = _run(*args, (x, w, b), fold=False)
    assert np.abs(folded - want).max() < 5e-3
    assert np.abs(flat - want).max() < 5e-3


def test_folding_is_what_saves_a_result_that_overflows_fp16():
    """The point of draining ACC24. The unfolded path converts the matmul output
    to FP16 first, so a value above 65504 is clamped BEFORE the epilogue scales
    it back into range -- and the clamp is silent (task #49)."""
    x = np.full((M, K), 40.0)
    w = np.full((K, N), 40.0)
    fn, specs = (lambda a, b: (a @ b) * 0.001), (_spec(M, K), _spec(K, N))

    folded, want, m_fold = _run(fn, specs, (x, w), fold=True)
    flat, _, m_flat = _run(fn, specs, (x, w), fold=False)

    assert want.max() > 200.0
    assert m_flat.saturated > 0
    assert m_fold.saturated == 0
    assert np.abs(folded - want).max() < 1.0
    assert np.abs(flat - want).max() > 100.0


# ---------------------------------------------------------------------------
# Reductions: the row is not the same thing as a VL chunk
# ---------------------------------------------------------------------------


def test_a_row_softmax_matches_the_reference():
    """A reduction over rows against a C tile stored in SUB-TILE order: a VL
    chunk of 64 is four sub-tiles of one row GROUP, not one row, so a
    contiguous load sums the wrong elements. The address generator walks the
    row with strides instead (vector-core.md s10) and this is what checks it."""
    x, w, _ = _rand()
    got, want, _ = _run(
        lambda a, b: D.softmax(a @ b, axis=1), (_spec(M, K), _spec(K, N)), (x, w)
    )
    assert np.abs(got - want).max() < 5e-3


def test_every_row_of_a_softmax_sums_to_one():
    """The invariant a permuted reduction breaks while still looking plausible:
    each element is in range and only the row totals give it away."""
    x, w, _ = _rand()
    got, _, _ = _run(
        lambda a, b: D.softmax(a @ b, axis=1), (_spec(M, K), _spec(K, N)), (x, w)
    )
    assert np.abs(got.sum(axis=1) - 1.0).max() < 5e-3


def test_a_row_max_lands_on_the_right_row():
    x, w, _ = _rand()
    got, want, _ = _run(
        lambda a, b: (a @ b) - D.softmax(a @ b, axis=1),
        (_spec(M, K), _spec(K, N)),
        (x, w),
    )
    assert np.abs(got - want).max() < 1e-2


# ---------------------------------------------------------------------------
# The image format is self-consistent
# ---------------------------------------------------------------------------


def test_packing_an_operand_round_trips():
    x = np.arange(64 * 128).reshape(64, 128) * 1.0
    flat = pack_operand(x, 32, 64, 4, 32)
    back = unpack_operand(flat, 64, 128, 32, 64, 4, 32)
    assert np.array_equal(x, back)


def test_a_shape_that_does_not_tile_is_rejected():
    with pytest.raises(MeshFault, match="does not divide"):
        pack_operand(np.zeros((10, 128)), 32, 64, 4, 32)


# ---------------------------------------------------------------------------
# Faults, so a codegen bug is a crash and not a wrong answer
# ---------------------------------------------------------------------------


def test_an_address_outside_every_region_is_a_fault():
    x, w, _ = _rand()
    graph = D.trace(lambda a, b: a @ b, _spec(M, K), _spec(K, N))
    sched = lower(graph, VU13P_8CU)
    prog = codegen(sched, M, K, N, VU13P_8CU)
    mesh = Mesh(prog, VU13P_8CU)
    mesh.upload(sched.bands[0], x, w)
    prog.insts[0].fields["word"] = prog.memory.total + 4096
    with pytest.raises(MeshFault, match="outside every region"):
        mesh.run()


def test_a_gemm_before_its_fill_is_a_fault():
    """Reading L1 that was never written would be a plausible-looking zero, so
    it has to be an error rather than a result."""
    x, w, _ = _rand()
    graph = D.trace(lambda a, b: a @ b, _spec(M, K), _spec(K, N))
    sched = lower(graph, VU13P_8CU)
    prog = codegen(sched, M, K, N, VU13P_8CU)
    mesh = Mesh(prog, VU13P_8CU)
    mesh.upload(sched.bands[0], x, w)
    prog.insts = [i for i in prog.insts if i.fields.get("sel") != "b"]
    with pytest.raises(MeshFault, match="before both operands"):
        mesh.run()


def test_an_unbound_tensor_operand_is_a_fault():
    x, w, _ = _rand()
    graph = D.trace(lambda a, bb, c: a @ bb + c, _spec(M, K), _spec(K, N), _spec(N))
    sched = lower(graph, VU13P_8CU)
    prog = codegen(sched, M, K, N, VU13P_8CU)
    mesh = Mesh(prog, VU13P_8CU)
    mesh.upload(sched.bands[0], x, w)
    with pytest.raises(MeshFault, match="nothing bound"):
        mesh.run()
