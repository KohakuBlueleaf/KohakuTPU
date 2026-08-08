"""Overhead is a number, and it is not allowed to grow quietly.

Every bound here was measured, not chosen. They exist because a restructure that
made the schedule "cleaner" silently added 40% more vector instructions and a
245,760-word scratch region for exactly the same arithmetic, and nothing in a
green suite noticed.
"""

import ktpu.dsl as D
from ktpu.codegen import codegen, cost_of
from ktpu.ir import FP16
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as T

M, K, N = 256, 1024, 256


def _cost(fn, *specs, fold=True):
    graph = D.trace(fn, *specs)
    sched = lower(graph, T, fold_epilogue=fold)
    return cost_of(codegen(sched, M, K, N, T), sched, T)


def _gelu():
    return _cost(
        lambda x, w, b: D.gelu(x @ w + b),
        D.TensorSpec((M, K), FP16),
        D.TensorSpec((K, N), FP16),
        D.TensorSpec((N,), FP16),
    )


def test_the_chain_costs_one_instruction_per_op_per_core():
    """16 ops on 8 cores is 128 VALU and nothing else scales with chain depth.
    A per-group memory round trip would make loads and stores scale with it
    too, which is what regressed."""
    c = _gelu()
    assert c.insts["valu"] == 16 * T.vector_cores
    assert c.insts["vld"] == 2 * T.vector_cores
    assert c.insts["vst"] == 1 * T.vector_cores
    assert c.insts["vloop"] == T.vector_cores


def test_the_vector_loop_body_does_not_scale_with_anything_but_the_ops():
    """One load of the tile, one broadcast of the bias, 16 ops, one store."""
    c = _gelu()
    assert c.vector_body == (16 + 2 + 1) * T.vector_cores


def test_no_scratch_region_exists():
    assert "tmp" not in _gelu().region_words


def test_the_image_holds_the_operands_the_result_and_nothing_spare():
    """A and B are 262,144 elements each, the staged matmul output and the
    result are 65,536 each, the bias is 256 -- all FP16, two per 32-bit word.
    Anything beyond that is overhead."""
    c = _gelu()
    want = 131072 + 131072 + 128 + 32768 + 32768
    assert c.image_words == want


def test_overhead_per_flop_stays_under_the_measured_bound():
    """0.016 before B residency was fixed, 0.027 after. The rise is CORRECT: a
    cluster only kept B across the m loop when it also owned every m tile of
    that column band, which it does not once the grid is dealt across cores --
    so the old figure was cheap because the fills were missing.

    Recovering it means dealing instances column-band major, so a core does own
    a whole column of tiles. That is a scheduling change, not a counting one.
    """
    c = _gelu()
    assert c.flops == 2 * M * K * N + 16 * M * N
    assert c.overhead < 0.03


def test_b_is_refilled_only_when_the_core_changes_column_band():
    """Skipping the fill on `mo != 0` fed a cluster L1 that was never written,
    which the level-3 simulator faults on rather than reading as zeros."""
    c = _gelu()
    assert c.insts["fill"] == 128


def test_folding_is_free_in_traffic_and_in_instructions():
    """It used to be believed the fold drained ACC24 at 1.5x FP16 and bought a
    single rounding for that price. Neither half was true: a DRAIN emits FP16
    (isa/cluster.md s5) and the accumulator converts at stage 6 on EMIT, so the
    intermediate is FP16 whether the epilogue is folded or not.

    What the fold actually does is give the epilogue the producer's tiles and
    grid, so it costs nothing and changes no precision."""
    specs = (
        D.TensorSpec((M, K), FP16),
        D.TensorSpec((K, N), FP16),
        D.TensorSpec((N,), FP16),
    )

    def fn(x, w, b):
        return D.gelu(x @ w + b)

    folded = _cost(fn, *specs, fold=True)
    flat = _cost(fn, *specs, fold=False)
    assert folded.dram_write == flat.dram_write
    assert folded.total_insts == flat.total_insts


def test_a_deeper_chain_adds_only_arithmetic():
    """Doubling the op count must add VALU and nothing else -- no extra loads,
    no extra stores, no extra region."""
    spec2 = (D.TensorSpec((M, N), FP16), D.TensorSpec((M, N), FP16))
    short = _cost(lambda x, y: x + y, *spec2)
    long = _cost(lambda x, y: D.gelu(x + y), *spec2)
    assert long.insts["valu"] > short.insts["valu"]
    assert long.insts["vld"] == short.insts["vld"]
    assert long.insts["vst"] == short.insts["vst"]
    assert long.image_words == short.image_words
