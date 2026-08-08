"""Is every part of the machine reachable from the DSL, and used correctly?

Three coverage questions, each a way the stack can quietly waste hardware:
an OpKind no frontend can emit, an ISA opcode codegen never produces, and an
op the reference simulator cannot execute.
"""

import itertools

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.codegen.cu import vmode_for
from ktpu.interp import run_one
from ktpu.ir import ELEMENTWISE, FP16, REDUCTION, VIEW, Graph, OpKind
from ktpu.ir.program import Opcode
from ktpu.ir.sched import Band, Engine, Grid, SchedOp, Schedule, Tile
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

S = D.TensorSpec((8, 16), FP16)
V = D.TensorSpec((16,), FP16)

#: Every OpKind, and the DSL expression that produces it.
FROM_DSL = {
    OpKind.NEG: lambda a, b: -a,
    OpKind.ABS: lambda a, b: abs(a),
    OpKind.RECIP: lambda a, b: D.recip(a),
    OpKind.RSQRT: lambda a, b: D.rsqrt(abs(a) + 1.0),
    OpKind.SQRT: lambda a, b: D.sqrt(abs(a)),
    OpKind.EXP2: lambda a, b: D.exp2(a),
    OpKind.LOG2: lambda a, b: D.log2(abs(a) + 1.0),
    OpKind.RELU: lambda a, b: D.relu(a),
    OpKind.ADD: lambda a, b: a + b,
    OpKind.SUB: lambda a, b: a - b,
    OpKind.MUL: lambda a, b: a * b,
    OpKind.DIV: lambda a, b: a / (abs(b) + 1.0),
    OpKind.MAX: lambda a, b: D.maximum(a, b),
    OpKind.MIN: lambda a, b: D.minimum(a, b),
    OpKind.CMPLT: lambda a, b: a < b,
    OpKind.CMPGT: lambda a, b: a > b,
    OpKind.CMPEQ: lambda a, b: a == b,
    OpKind.FMA: lambda a, b: D.fma(a, b, a),
    OpKind.SELECT: lambda a, b: D.where(a > b, a, b),
    OpKind.SUM: lambda a, b: a.sum(axis=-1),
    OpKind.RMAX: lambda a, b: a.max(axis=-1),
    OpKind.RMIN: lambda a, b: a.min(axis=-1),
    OpKind.SUMSQ: lambda a, b: a.sumsq(axis=-1),
    OpKind.MATMUL: lambda a, b: a @ a.transpose(),
    OpKind.RESHAPE: lambda a, b: a.reshape(16, 8),
    OpKind.PERMUTE: lambda a, b: a.permute(1, 0),
    OpKind.EXPAND: lambda a, b: a.sum(axis=-1, keepdim=True).expand(8, 16),
    OpKind.SLICE: lambda a, b: a[0:4],
    OpKind.PAD: lambda a, b: a.pad(((1, 1), (0, 0))),
    OpKind.CONCAT: lambda a, b: a.concat(b, axis=0),
    OpKind.CAST: lambda a, b: a.to(D.NUMPY_DTYPES and FP16),
    OpKind.INPUT: lambda a, b: a,
    OpKind.CONST: lambda a, b: a + 2.0,
}


@pytest.mark.parametrize("kind", sorted(OpKind, key=lambda k: k.value))
def test_every_opkind_is_reachable_from_the_dsl(kind):
    """An OpKind no frontend can emit is dead weight in the IR."""
    assert kind in FROM_DSL, f"{kind.value} has no DSL expression"
    g = D.trace(FROM_DSL[kind], S, S)
    assert any(op.kind is kind for op in g.ops), f"{kind.value} was not emitted"


@pytest.mark.parametrize("kind", sorted(OpKind, key=lambda k: k.value))
def test_every_opkind_executes_in_the_reference_simulator(kind):
    """An op the reference cannot run cannot be checked against anything."""
    g = D.trace(FROM_DSL[kind], S, S)
    a, b = np.random.default_rng(3).normal(size=(2, 8, 16))
    out = run_one(g, a, b)
    assert np.isfinite(out).all()


def test_the_op_sets_partition_cleanly():
    """Every op is elementwise, a reduction, a view, or one of four singletons."""
    named = ELEMENTWISE | REDUCTION | VIEW
    rest = set(OpKind) - named
    assert rest == {OpKind.INPUT, OpKind.CONST, OpKind.MATMUL, OpKind.CAST}


# ---------------------------------------------------------------------------
# level 3: is every instruction the codegen can emit actually emitted?
# ---------------------------------------------------------------------------

MATMUL_OPS = {Opcode.FILL, Opcode.GEMM, Opcode.DRAIN}
VECTOR_OPS = {
    Opcode.VSETVL,
    Opcode.VSETMD,
    Opcode.VLOOP,
    Opcode.VLD,
    Opcode.VALU,
    Opcode.VST,
}


def _prog_for(fn, *specs, m=64, k=64, n=64):
    return codegen(lower(D.trace(fn, *specs), VU13P_8CU), m, k, n, VU13P_8CU)


def test_a_matmul_kernel_emits_every_cluster_opcode():
    prog = _prog_for(
        lambda a, b: a @ b, D.TensorSpec((64, 64), FP16), D.TensorSpec((64, 64), FP16)
    )
    assert {i.op for i in prog.insts} >= MATMUL_OPS


def test_an_elementwise_kernel_emits_every_vector_opcode():
    prog = _prog_for(lambda a, b: D.gelu(a + b), S, S, m=8, k=16, n=16)
    got = {i.op for i in prog.insts}
    assert got >= VECTOR_OPS - {Opcode.VRED}


def test_a_reduction_kernel_emits_vred_and_selects_tree_mode():
    g = D.trace(lambda a, b: a.sum(axis=-1), S, S)
    sched = lower(g, VU13P_8CU)
    vec = [b for b in sched.bands if b.engine is Engine.VECTOR]
    assert vec and vmode_for(vec[0])[0] == "TREE"
    prog = codegen(sched, 8, 16, 16, VU13P_8CU)
    assert Opcode.VRED in {i.op for i in prog.insts}


@pytest.mark.parametrize(
    "n_ops,mode,depth",
    [(1, "FLAT", 1), (2, "D2", 2), (3, "D2", 2), (4, "D4", 4), (9, "D4", 4)],
)
def test_chain_depth_follows_the_op_count(n_ops, mode, depth):
    """FLAT is memory-bound, D2 saturates the ALUs, D4 leaves headroom."""
    band = Band(
        engine=Engine.VECTOR,
        grid=Grid(m=1),
        tile=Tile(m=256),
        ops=[SchedOp(OpKind.MUL) for _ in range(n_ops)],
    )
    assert vmode_for(band) == (mode, depth)


def test_the_loop_body_covers_the_whole_instance():
    """Chunks share one sequence, so the body is issued once inside VLOOP."""
    prog = _prog_for(lambda a, b: D.gelu(a + b), S, S, m=8, k=16, n=16)
    loops = [i for i in prog.insts if i.op is Opcode.VLOOP]
    assert loops
    for lp in loops:
        assert lp.fields["count"] >= 1
        assert lp.fields["body"] >= 3


def test_instances_never_name_more_nodes_than_the_target_has():
    """A band of 64 instances over 8 cores must name 8 nodes, not 64."""
    prog = _prog_for(lambda a, b: D.gelu(a + b), S, S, m=8, k=16, n=16)
    assert len(prog.nodes(Engine.MATMUL)) <= VU13P_8CU.clusters
    assert len(prog.nodes(Engine.VECTOR)) <= VU13P_8CU.vector_cores


def test_a_target_without_an_engine_is_refused_not_silently_dropped():
    from ktpu.target import VU13P_MM8

    g = D.trace(lambda a, b: a + b, S, S)
    with pytest.raises(ValueError, match="has none"):
        codegen(lower(g, VU13P_MM8), 8, 16, 16, VU13P_MM8)


def test_host_bands_are_reported_not_hidden():
    """Work neither core can do must be visible, not assumed supported."""
    s = Schedule()
    s.add(Band(Engine.HOST, Grid(m=1), Tile(m=8), ops=[SchedOp(OpKind.CONCAT)]))
    assert len(s.host_bands()) == 1
    assert not Engine.HOST.on_device


def test_memory_regions_are_all_in_32_bit_words():
    """A/B/C must share one unit; mixing words and sub-tiles hides a 8x error."""
    prog = _prog_for(
        lambda a, b: a @ b,
        D.TensorSpec((64, 64), FP16),
        D.TensorSpec((64, 64), FP16),
    )
    for name, elems in (("a", 64 * 64), ("b", 64 * 64), ("c", 64 * 64)):
        r = prog.memory.get(name)
        assert r.elems == elems
        assert r.words >= elems // 2
        assert r.note


def test_regions_do_not_overlap():
    prog = _prog_for(
        lambda a, b: a @ b,
        D.TensorSpec((64, 64), FP16),
        D.TensorSpec((64, 64), FP16),
    )
    spans = [(r.word, r.end) for r in prog.memory.regions]
    for (a0, a1), (b0, b1) in itertools.pairwise(spans):
        assert a1 <= b0, f"{(a0, a1)} overlaps {(b0, b1)}"


def test_every_instruction_addresses_inside_a_region():
    prog = _prog_for(
        lambda a, b: a @ b,
        D.TensorSpec((64, 64), FP16),
        D.TensorSpec((64, 64), FP16),
    )
    total = prog.memory.total
    for i in prog.insts:
        if "word" in i.fields:
            assert 0 <= i.fields["word"] < total, str(i)


def test_rounds_partition_the_instruction_stream():
    prog = _prog_for(lambda a, b: D.gelu(a + b), S, S, m=8, k=16, n=16)
    flat = [i for r in prog.rounds for i in r]
    assert flat == list(range(len(prog.insts)))
    assert all(len(r) <= VU13P_8CU.stage_flits for r in prog.rounds)


def test_a_graph_with_no_ops_still_lowers():
    g = Graph("empty")
    g.output(g.input((4,), FP16, name="x"))
    sched = lower(g, VU13P_8CU)
    assert sched.bands == []
