"""The unit models, against the arithmetic they claim to predict.

The model executes the artifact the driver dispatches, so a disagreement here is
a compiler or encoding fault. Its agreement with `mxfp7.model_matmul` is what
pins the addressing: an L1 offset, a bank or a sub-tile order that was wrong
would show as an error of order one, not of order 1e-4.
"""

import numpy as np
import pytest
from kohakuaccel.sim import Memory
from kohakutpu.hw import mxfp7
from kohakutpu.hw import vector as V
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa import ISA
from kohakutpu.isa.vecemit import (
    BATCH_BYTES,
    BATCH_ELEMS,
    OUT_REG,
    ElementwiseKernel,
    RowReduceKernel,
)
from kohakutpu.kernels import linear_silu, rmsnorm, softmax
from kohakutpu.model import (
    FP16_MAX,
    ClusterUnit,
    ModelError,
    SimDevice,
    VecFault,
    VectorUnit,
    to_e8m15,
)
from kohakutpu.ops import matmul, silu

M = K = N = 64


def operands(seed=0, scale=0.5):
    rng = np.random.default_rng(seed)
    a = (rng.standard_normal((M, K)) * scale).astype(np.float16)
    b = (rng.standard_normal((N, K)) * scale).astype(np.float16)
    return a, b


def run(a, b, **tiling):
    """Compile and execute `matmul`, returning (result, device)."""
    dev = SimDevice()
    return matmul(dev.tensor(a), dev.tensor(b), **tiling).numpy(), dev


def test_the_model_matches_the_mxfp7_reference():
    """Every address, offset, bank and sub-tile position, checked at once."""
    a, b = operands()
    got, _ = run(a, b)
    want = mxfp7.model_matmul(a, b)
    assert np.abs(got - want).max() / np.abs(want).max() < 1e-3


def test_it_executes_the_whole_grid():
    """128 instances of four instructions, over six clusters and four rounds.

    The TILING IS PINNED, not defaulted: this is about a grid being executed
    whole, and at the shipped 8x8 a 64x64 matmul is one instance and exercises
    no distribution at all.
    """
    a, b = operands()
    _, dev = run(a, b, gm=2, gn=1)
    ran = [u.counts for u in dev.clusters]
    assert sum(c["GEMM"] for c in ran) == 128
    assert sum(c["DRAIN"] for c in ran) == 128
    assert sum(c["FILL"] for c in ran) == 256
    assert dev.stats()["rounds"] == 4


def test_the_shipped_tiling_computes_the_same_answer():
    """The default is 8x8; every earlier number here was measured at 2x1."""
    a, b = operands()
    wide, _ = run(a, b)
    narrow, _ = run(a, b, gm=2, gn=1)
    assert np.array_equal(wide, narrow)


@pytest.mark.parametrize("seed", range(4))
def test_well_scaled_operands_never_reach_the_fp16_ceiling(seed):
    """The card returns 0x7BFF words on these shapes; the model does not."""
    got, dev = run(*operands(seed))
    assert dev.saturated == 0, dev.clamped
    assert not np.isinf(got).any()
    assert int((got.astype(np.float16).view(np.uint16) == 0x7BFF).sum()) == 0
    assert np.abs(got).max() < 16.0


def test_the_drain_reports_a_clamp_when_one_really_happens():
    """The negative control: without it a clean run proves nothing."""
    a, b = operands(scale=300.0)
    got, dev = run(a, b)
    assert dev.saturated > 0
    assert np.abs(got).max() == FP16_MAX
    addr, elem = dev.clamped[0]
    assert 0 <= elem < 16 and addr % 32 == 0


@pytest.mark.parametrize("k", [128, 256])
def test_a_sweep_over_several_k_chunks_is_right(k):
    """K past one chunk is where the L1 banks start alternating.

    At K=64 with nk=2 there is exactly one chunk, so `fbank` and `abank` are
    always zero and nothing about banking is exercised at all.
    """
    rng = np.random.default_rng(0)
    a = (rng.standard_normal((M, k)) * 0.5).astype(np.float16)
    b = (rng.standard_normal((N, k)) * 0.5).astype(np.float16)
    got, dev = run(a, b)
    want = mxfp7.model_matmul(a, b)
    assert np.abs(got - want).max() / np.abs(want).max() < 1e-3
    assert dev.saturated == 0


def test_a_fill_into_the_running_sweep_is_refused():
    """The hazard no serial reading of the program can see.

    A GEMM retires when its sweep starts, so a FILL that lands on entries it is
    still reading corrupts sub-tiles and reports nothing. This is the defect an
    encoder that never alternated the bank per K chunk produced.
    """
    unit = ClusterUnit(mem_base=0)
    mem = Memory(1 << 16)
    same = {"sel": 0, "eoff": 0, "fbank": 0}
    unit.execute(ISA.fill(addr=0, n=4, **same), mem)
    unit.execute(ISA.gemm(gm=2, gn=1, nk=2, abank=0, bbank=0), mem)
    with pytest.raises(ModelError, match="sweep in flight"):
        unit.execute(ISA.fill(addr=0, n=4, **same), mem)


def test_the_other_bank_is_free_while_a_sweep_runs():
    """Which is what alternating per chunk buys, and why it is not cosmetic."""
    unit = ClusterUnit(mem_base=0)
    mem = Memory(1 << 16)
    unit.execute(ISA.fill(addr=0, n=4, sel=0, eoff=0, fbank=0), mem)
    unit.execute(ISA.gemm(gm=2, gn=1, nk=2, abank=0, bbank=0), mem)
    unit.execute(ISA.fill(addr=0, n=4, sel=0, eoff=0, fbank=1), mem)


# ------------------------------------------------------------ the vector core
FP16 = np.float16
#: Room for the whole-batch write past a shorter allocation, twice over.
SLAB = 2 * BATCH_BYTES


def core(image, descs=(), pc=0, mem=None):
    """A unit with `image` loaded and `descs` set, and the memory it ran on."""
    unit = VectorUnit(mem_base=0)
    mem = Memory(1 << 22) if mem is None else mem
    for at, word in enumerate(image):
        unit.execute(V.imem_flit(at, word), mem)
    for flit in descs:
        unit.execute(flit, mem)
    unit.execute(V.run_flit(pc), mem)
    return unit, mem


def elementwise(chain, arrays, nelem=None):
    """Run one :class:`ElementwiseKernel` over `arrays`. Returns (out, unit)."""
    unit = VectorUnit(mem_base=0)
    mem = Memory(1 << 22)
    srcs = [i * SLAB for i in range(len(arrays))]
    for at, a in zip(srcs, arrays):
        mem.write(at, np.asarray(a, FP16).tobytes())
    dst = len(arrays) * SLAB
    n = arrays[0].size if nelem is None else nelem
    kernel = ElementwiseKernel(chain, len(arrays))
    for flit in kernel.flits(srcs, dst, n):
        unit.execute(flit, mem)
    return np.frombuffer(mem.read(dst, n * 2), FP16).astype(np.float64), unit


def rows_of(kind, x):
    """Run a :class:`RowReduceKernel` over `x`. Returns the broadcast result."""
    rows, cols = x.shape
    unit = VectorUnit(mem_base=0)
    mem = Memory(1 << 22)
    mem.write(0, np.asarray(x, FP16).tobytes())
    for flit in RowReduceKernel(kind, rows, cols).flits(0, SLAB):
        unit.execute(flit, mem)
    got = np.frombuffer(mem.read(SLAB, rows * cols * 2), FP16)
    return got.astype(np.float64).reshape(rows, cols)


def operand(seed, size=BATCH_ELEMS, scale=2.0, positive=False):
    rng = np.random.default_rng(seed)
    got = rng.standard_normal(size) * scale
    return np.asarray(np.abs(got) + 0.5 if positive else got, FP16).astype(np.float64)


#: Each op, the operands it is safe on, and what it should compute.
UNARY = {
    OpKind.NEG: np.negative,
    OpKind.ABS: np.abs,
    OpKind.EXP2: np.exp2,
    OpKind.LOG2: np.log2,
    OpKind.RECIP: lambda a: 1.0 / a,
    OpKind.RSQRT: lambda a: 1.0 / np.sqrt(a),
}
BINARY = {
    OpKind.MUL: np.multiply,
    OpKind.ADD: np.add,
    OpKind.SUB: np.subtract,
}


@pytest.mark.parametrize("kind", list(UNARY), ids=lambda k: k.value)
def test_every_unary_op_is_the_function_it_names(kind):
    """Bit for bit: the lane rounds once, so there is nothing to be near."""
    x = operand(2, positive=True)
    got, _ = elementwise([(kind, [0])], [x])
    want = np.asarray(to_e8m15(UNARY[kind](x))).astype(FP16).astype(np.float64)
    assert (got == want).all(), np.abs(got - want).max()


@pytest.mark.parametrize("kind", list(BINARY), ids=lambda k: k.value)
def test_every_binary_op_reads_the_slot_the_isa_reads(kind):
    """A wrong operand slot is a legal word computing something else.

    SUB is what pins it: `a - b` and `b - a` are both plausible and only one is
    what `vec_alu.v` puts in `vc`.
    """
    x, y = operand(3), operand(4, positive=True)
    got, _ = elementwise([(kind, [0, 1])], [x, y])
    want = np.asarray(to_e8m15(BINARY[kind](x, y))).astype(FP16).astype(np.float64)
    assert (got == want).all(), np.abs(got - want).max()


def test_a_divide_is_a_reciprocal_and_a_multiply():
    """The negative control for the two above: DIV is NOT one rounded divide.

    `lower_op` emits VINV then VMUL, so a quotient carries two E8M15 roundings
    and a model that computed `a / b` would be wrong in the last bit.
    """
    x, y = operand(3), operand(4, positive=True)
    got, _ = elementwise([(OpKind.DIV, [0, 1])], [x, y])
    once = np.asarray(to_e8m15(x / y)).astype(FP16).astype(np.float64)
    twice = to_e8m15(x * np.asarray(to_e8m15(1.0 / y)))
    assert (got == np.asarray(twice).astype(FP16).astype(np.float64)).all()
    assert (
        got != once
    ).any(), "a two-rounding quotient cannot equal a one-rounding one"


def test_the_lane_carries_sixteen_significand_bits():
    """`(1 + 2^-16) - 1` is zero on this core and `2^-16` in exact arithmetic.

    The intermediate is where the lane's precision shows: fp16 is coarser than
    E8M15, so a single op's result cannot demonstrate it.
    """
    one = np.ones(BATCH_ELEMS)
    chain = [(OpKind.ADD, [0, 1]), (OpKind.SUB, [OUT_REG, 0])]
    lost, _ = elementwise(chain, [one, np.full(BATCH_ELEMS, 2.0**-16)])
    kept, _ = elementwise(chain, [one, np.full(BATCH_ELEMS, 2.0**-15)])
    assert (lost == 0.0).all()
    assert (kept == 2.0**-15).all()


# Two tests here cross-checked this model against `ktpu.interp.mesh`. They
# agreed, and that is in git; a copy in this tree would only check itself.


@pytest.mark.parametrize("cols", [16, 64, 128])
def test_a_row_maximum_is_exact_and_broadcast(cols):
    """MAX picks an operand, so the tree cannot round: any error is addressing."""
    x = operand(6, size=8 * cols).reshape(8, cols)
    got = rows_of(OpKind.RMAX, x)
    assert (got == np.repeat(x.max(axis=1, keepdims=True), cols, axis=1)).all()


def test_a_row_sum_carries_the_tree_s_rounding_and_not_more():
    """The sum is a tree of E8M15 adds, so it is near an exact sum, not equal.

    Bounded BELOW as well: a model that summed in float64 would pass a tolerance
    check and hide the precision the card actually has.
    """
    x = operand(7, size=8 * 128).reshape(8, 128)
    got = rows_of(OpKind.SUM, x)
    want = x.sum(axis=1)
    err = np.abs(got[:, 0] - want)
    assert (got == got[:, :1]).all(), "the row is not broadcast"
    assert err.max() < 1e-3 * np.abs(want).max()
    assert err.max() > 0.0, "a float64 sum is not what this core computes"


def test_a_pass_writes_a_whole_batch_past_a_shorter_allocation():
    """One RUN drains `BATCH_ELEMS` whatever `nelem` says, which is why the
    arena aligns to `BATCH_BYTES`."""
    keep = 64
    unit = VectorUnit(mem_base=0)
    mem = Memory(1 << 22)
    mem.write(0, np.asarray(operand(8), FP16).tobytes())
    mem.write(SLAB, b"\xaa" * BATCH_BYTES)
    kernel = ElementwiseKernel([(OpKind.NEG, [0])], 1)
    for flit in kernel.flits([0], SLAB, keep):
        unit.execute(flit, mem)
    after = mem.read(SLAB + keep * 2, (BATCH_ELEMS - keep) * 2)
    assert after != b"\xaa" * len(after)


def test_only_the_alu_has_a_tail_mask():
    """A load and a store walk WHOLE chunks; only the write-back honours VL.

    Nothing shipped sets a VL off a 16-boundary, so this is the only place the
    tail is exercised -- and a model that masked the store instead would look
    right on every kernel that exists and be wrong on the first one that does.
    """
    x = operand(9, size=32)
    mem = Memory(1 << 22)
    mem.write(0, np.asarray(x, FP16).tobytes())
    image = [
        V.vseti(0),
        20,
        V.vsetvl(0),
        V.vsetmode(V.FLAT),
        V.vfill(0, 0),
        V.vbar(),
        V.vld(0, 1, 0),
        V.vld(1, 1, 0),
        V.alu("VNEG", vd=1, va=0),
        V.vst(1, 2, 0),
        V.vdrain(3, 2),
        V.vhalt(),
    ]
    descs = [
        V.desc_flit(0, 1, V.dim(V.WORD_BYTES, 2)),
        V.desc_flit(1, 0, 0),
        V.desc_flit(1, 1, V.dim(1, 2)),
        V.desc_flit(2, 0, 2),
        V.desc_flit(2, 1, V.dim(1, 2)),
        V.desc_flit(3, 0, SLAB),
        V.desc_flit(3, 1, V.dim(V.WORD_BYTES, 2)),
    ]
    core(image, descs, mem=mem)
    got = np.frombuffer(mem.read(SLAB, 32 * 2), FP16).astype(np.float64)
    assert (got[:20] == -x[:20]).all(), "the ALU wrote fewer lanes than VL"
    assert (got[20:] == x[20:]).all(), "the ALU wrote past VL, or the store masked"


def test_a_store_that_leaves_fp16_saturates_and_is_counted():
    """The negative control: a clean run over well-scaled data proves nothing."""
    big = np.full(BATCH_ELEMS, 300.0)
    got, unit = elementwise([(OpKind.MUL, [0, 0])], [big])
    assert unit.saturated == BATCH_ELEMS
    assert (got == FP16_MAX).all()


def test_an_alu_instruction_left_in_tree_mode_is_refused():
    """A chained instruction that is not a VRED faults on the card; guessing
    what four gathered words compute would be a plausible wrong answer."""
    image = [V.vsetmode(V.TREE), V.alu("VMUL", vd=1, va=0, vb=0), V.vhalt()]
    with pytest.raises(ModelError, match="ONE chain"):
        core(image)


def test_a_reduction_over_a_partial_chunk_faults():
    """F_REDVL: a partial chunk feeds stale slots into a tree with no mask."""
    image = [
        V.vseti(0),
        24,
        V.vsetvl(0),
        V.vsetmode(V.TREE),
        V.vred(1, 0, "SUM"),
        V.vhalt(),
    ]
    with pytest.raises(VecFault, match="F_REDVL"):
        core(image)


def test_a_reduction_outside_tree_mode_faults():
    """F_OPCODE, which is what `reduce_row` switches the mode back to avoid."""
    image = [V.vsetmode(V.FLAT), V.vred(1, 0, "SUM"), V.vhalt()]
    with pytest.raises(VecFault, match="F_OPCODE"):
        core(image)


def test_a_walk_longer_than_the_descriptor_faults():
    """F_LEN at 256 entries, before the walk wraps L1 into somebody's operand."""
    image = [V.vfill(0, 0), V.vhalt()]
    descs = [V.desc_flit(0, 1, V.dim(V.WORD_BYTES, 300))]
    with pytest.raises(VecFault, match="F_LEN"):
        core(image, descs)


# ------------------------------------------------------- whole kernels, no card
def error_of(got, want) -> float:
    """Largest error relative to the reference's own scale."""
    got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
    return float(np.abs(got - want).max() / np.abs(want).max())


def inputs(seed=0, shape=(32, 64), scale=1.0):
    rng = np.random.default_rng(seed)
    return np.asarray(rng.standard_normal(shape) * scale, np.float16)


def test_silu_runs_offline():
    """`x * sigmoid(x)`, against a float64 reference. One fp16 ulp is 5e-4."""
    x = inputs(scale=2.0)
    got = silu(SimDevice().tensor(x)).numpy()
    ref = x.astype(np.float64)
    assert error_of(got, ref / (1.0 + np.exp(-ref))) < 1e-3


def test_rmsnorm_runs_offline():
    """A row reduction and three elementwise passes, end to end."""
    x, w = inputs(scale=2.0), inputs(1, scale=0.5) + 1.0
    dev = SimDevice()
    got = rmsnorm(dev.tensor(x), dev.tensor(w)).numpy()
    xf, wf = x.astype(np.float64), np.asarray(w, np.float64)
    want = xf / np.sqrt((xf**2).mean(axis=1, keepdims=True) + 1e-5) * wf
    assert error_of(got, want) < 2e-3


def test_softmax_runs_offline():
    """Two reductions and the transcendental between them."""
    x = inputs(scale=2.0)
    got = softmax(SimDevice().tensor(x)).numpy()
    e = np.exp(x.astype(np.float64) - x.astype(np.float64).max(axis=1, keepdims=True))
    assert error_of(got, e / e.sum(axis=1, keepdims=True)) < 2e-3


def test_the_fused_linear_silu_runs_offline():
    """A cluster stage and a vector stage, joined by a drain over the NoC.

    Checked against MXFP7 rather than float64: the matmul's quantisation is the
    dominant error and it is the cluster's, not this core's.
    """
    a, b = inputs(2, (32, 64), 0.3), inputs(3, (64, 64), 0.3)
    dev = SimDevice()
    got = linear_silu(dev.tensor(a), dev.tensor(b)).numpy()
    held = mxfp7.model_matmul(a, b)
    assert error_of(got, held / (1.0 + np.exp(-held))) < 1e-3
    assert dev.saturated == 0


def test_the_fused_and_staged_routes_agree_to_a_constant_s_precision():
    """Both compute the same thing; they differ only in how the fold is stored.

    A folded scalar is an fp16 broadcast array on the staged route and an
    E8M15 scalar register on the fused one, so they are close and not equal.
    """
    a, b = inputs(2, (32, 64), 0.3), inputs(3, (64, 64), 0.3)
    one, two = SimDevice(), SimDevice()
    fused = linear_silu(one.tensor(a), one.tensor(b)).numpy()
    apart = linear_silu(two.tensor(a), two.tensor(b), fuse=False).numpy()
    assert error_of(fused, apart) < 5e-3


def test_a_node_addressed_drain_reaches_the_core_that_reads_it():
    """The fused epilogue's whole premise: the tile arrives without DRAM."""
    a, b = inputs(2, (32, 64), 0.3), inputs(3, (64, 64), 0.3)
    dev = SimDevice()
    linear_silu(dev.tensor(a), dev.tensor(b))
    assert sum(u.counts["RUN"] for u in dev.cores) == 2
    assert sum(u.counts["DRAIN"] for u in dev.clusters) == 2
