"""The row-wise kernels past one reduction pass, and the wall that remains.

A DiT at 512x512 has a 1024-wide model dim, so these shapes are the target's and
not a stress test. Graded against float64 relative to the true peak.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims as _dims
from kohakuaccel.lang import units
from kohakutpu.hw import vector as V
from kohakutpu.hw.ops import OpKind
from kohakutpu.hw.veckernels import imem_flits
from kohakutpu.isa.vecemit import (
    BandKernel,
    Chain,
    ElementwiseKernel,
    Spread,
    VecEmitError,
)
from kohakutpu.kernels import (
    layernorm,
    layernorm_wide,
    rmsnorm,
    rmsnorm_wide,
    softmax,
    softmax_wide,
    split,
)
from kohakutpu.kernels.groupnorm import VLMAX, fold
from kohakutpu.kernels.wide import fold_flat
from kohakutpu.lang import kernel
from kohakutpu.lang.backend import BACKEND, _held, _per, _span
from kohakutpu.model import Memory, SimDevice, VectorUnit

from kohakutpu import lang as L

FP16 = np.float16

R_, W_ = _dims("R_, W_")


@kernel
def layernorm_staged(
    x=L.In(..., R_, W_),
    w=L.In(..., R_, W_),
    b=L.In(..., R_, W_),
    y=L.Out(..., R_, W_),
    *,
    eps=1e-5,
    part=8192,
):
    """The staged reference the wide form is graded against. A FIXTURE.

    It lived in `kernels/norm.py` in the tensor-level shape this repo no longer
    allows: a bare `y <<=` states no tiling and buys a DRAM round trip a kernel
    should not spend. Kept here because grading wants something slow and
    obviously right to compare against.
    """
    total, mu, dev = L.temp(R_, W_), L.temp(R_, W_), L.temp(R_, W_)
    sq, var, inv, scaled = (L.temp(R_, W_) for _ in range(4))
    with units(x.parts(part)) as e:
        total[e] <<= L.row_sum(x[e])
    with units(x.parts(part)) as e:
        mu[e] <<= total[e] / W_
    with units(x.parts(part)) as e:
        dev[e] <<= x[e] - mu[e]
    with units(x.parts(part)) as e:
        sq[e] <<= (dev[e] / W_) * dev[e]
    with units(x.parts(part)) as e:
        var[e] <<= L.row_sum(sq[e])
    with units(x.parts(part)) as e:
        inv[e] <<= L.rsqrt(var[e] + eps)
    with units(x.parts(part)) as e:
        scaled[e] <<= dev[e] * inv[e]
    with units(x.parts(part)) as e:
        y[e] <<= scaled[e] * w[e] + b[e]


def rel(got, want) -> float:
    """Largest error as a fraction of the reference's own peak."""
    want = np.asarray(want, np.float64)
    return float(np.abs(np.asarray(got, np.float64) - want).max() / np.abs(want).max())


def operands(shape, shift=0.0, seed=4):
    rng = np.random.default_rng(seed)
    x = np.asarray(rng.standard_normal(shape) + shift, FP16)
    w = np.asarray(rng.standard_normal(shape) * 0.2 + 1, FP16)
    return x, w, np.asarray(rng.standard_normal(shape) * 0.2, FP16)


def ln_ref(x, w, b, eps=1e-5):
    v = np.asarray(x, np.float64)
    mu = v.mean(-1, keepdims=True)
    var = ((v - mu) ** 2).mean(-1, keepdims=True)
    return (v - mu) / np.sqrt(var + eps) * np.float64(w) + np.float64(b)


def rms_ref(x, w, eps=1e-5):
    v = np.asarray(x, np.float64)
    return v / np.sqrt((v * v).mean(-1, keepdims=True) + eps) * np.float64(w)


def sm_ref(x):
    v = np.asarray(x, np.float64)
    e = np.exp(v - v.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


def rows_of(dev, a, cols):
    """`a` handed over as the row split the wide kernels take."""
    rows = split(cols)
    return dev.tensor(a.reshape(a.shape[0], rows, cols // rows))


# ----------------------------------------------------------- the three kernels
@pytest.mark.parametrize("cols", [128, 1024, 4096])
def test_layernorm_folds_a_row_no_single_pass_reaches(cols):
    """The DiT's own shape. 1024 is where every layernorm in the target sits."""
    m = 16
    x, w, b = operands((m, cols))
    dev = SimDevice(size=1024 << 20)
    got = layernorm_wide(
        rows_of(dev, x, cols),
        rows_of(dev, w, cols),
        rows_of(dev, b, cols),
        rows=split(cols),
    ).numpy()
    assert rel(got.reshape(m, cols), ln_ref(x, w, b)) < 3e-3
    assert dev.saturated == 0


@pytest.mark.parametrize("cols", [128, 1024, 4096])
def test_rmsnorm_folds_a_row_no_single_pass_reaches(cols):
    m = 16
    x, w, _ = operands((m, cols))
    dev = SimDevice(size=1024 << 20)
    got = rmsnorm_wide(rows_of(dev, x, cols), rows_of(dev, w, cols), rows=split(cols))
    assert rel(got.numpy().reshape(m, cols), rms_ref(x, w)) < 3e-3
    assert dev.saturated == 0


@pytest.mark.parametrize("cols", [128, 1024, 4096])
def test_softmax_folds_BOTH_the_max_and_the_sum(cols):
    """`row_max` reaches one sub-row, so the maximum needs the hierarchy too.

    Folding only the sum leaves `x - hi` positive somewhere and `exp2` runs
    away, which is the shape of failure this checks for.
    """
    m = 16
    x, _, _ = operands((m, cols))
    dev = SimDevice(size=1024 << 20)
    got = softmax_wide(rows_of(dev, x, cols), rows=split(cols)).numpy()
    assert rel(got.reshape(m, cols), sm_ref(x)) < 3e-3
    assert dev.saturated == 0


def test_a_max_that_is_NOT_folded_across_the_sub_rows_would_be_caught():
    """The reference the test above rests on: a per-sub-row max is a wrong answer.

    Written down because the fold is invisible in the result -- a softmax still
    sums to one when the maximum came from the wrong sub-row.
    """
    x = np.asarray(np.arange(1024).reshape(8, 128) * 0.1, FP16)
    per_row = np.exp(np.float64(x) - np.float64(x).max(-1, keepdims=True))
    whole = np.exp(np.float64(x).ravel() - np.float64(x).max())
    assert rel(per_row.ravel() / per_row.sum(), whole / whole.sum()) > 0.5


# ------------------------------------------------- the wide path is a BRANCH
@pytest.mark.parametrize("cols", [128])
def test_the_narrow_arithmetic_is_UNCHANGED_at_one_sub_row(cols):
    """At `rows == 1` the fold is empty, so the wide path is the narrow one.

    Against `layernorm_staged`, which is the arithmetic these kernels were
    written from. Since `fused.py` was promoted the SHIPPED `layernorm` is one
    band with no intermediate in DRAM and is the more accurate of the two --
    `wide.py` owes it that rewrite.

    Equal ERROR rather than equal bits: reduce/apply grouping cuts the two into
    a different number of programs, so they round in different places and land
    on the same number. An empty fold that walked the wrong `vl` would not.
    """
    m = 16
    x, w, b = operands((m, cols))
    dev = SimDevice(size=1024 << 20)
    wide = layernorm_wide(
        rows_of(dev, x, cols), rows_of(dev, w, cols), rows_of(dev, b, cols), rows=1
    ).numpy()
    narrow = layernorm_staged(dev.tensor(x), dev.tensor(w), dev.tensor(b)).numpy()
    truth = ln_ref(x, w, b)
    got = wide.reshape(m, cols)
    assert rel(got, narrow) < 1e-2, "the wide path is not the narrow arithmetic"
    assert abs(rel(got, truth) - rel(narrow, truth)) < 1e-6


def test_the_row_SPLIT_moves_no_bytes(cols=1024):
    """The reshape that reaches these kernels is a view, or it is a relayout.

    A batch stride over the block would need the caller's array packed into the
    padding, which is a host copy per call. At a whole `BATCH_ELEMS` row it is
    exactly the block, so the split is the same bytes under another shape.
    """
    rows = split(cols)
    dev = SimDevice(size=64 << 20)
    zero = np.zeros((8, rows, cols // rows), FP16)
    got = layernorm_wide.plan(
        dev.tensor(zero), dev.tensor(zero), dev.tensor(zero), rows=rows
    )
    strides = {getattr(lay, "stride", None) for lay in got.layouts.values()}
    assert strides - {None} == {cols * 2}


def test_the_shipped_kernels_still_refuse_a_wide_row(cols=1024):
    """The wide path is a NEW branch. The narrow ones are untouched and refuse."""
    x, w, b = operands((8, cols))
    dev = SimDevice(size=64 << 20)
    for call in (
        lambda: softmax(dev.tensor(x)),
        lambda: rmsnorm(dev.tensor(x), dev.tensor(w)),
        lambda: layernorm(dev.tensor(x), dev.tensor(w), dev.tensor(b)),
    ):
        with pytest.raises(Exception, match="VRED needs a multiple"):
            call()


def test_the_refusal_names_the_branch_that_is_open():
    """A refusal that names a shut door is worth less than nothing."""
    x, _, _ = operands((8, 1024))
    dev = SimDevice(size=64 << 20)
    with pytest.raises(Exception, match="kernels.wide"):
        softmax(dev.tensor(x))


# ------------------------------------------------------- what still refuses
def test_a_row_that_is_not_whole_sub_rows_is_refused():
    """A partial sub-row is a partial pass, and the block behind it wins."""
    with pytest.raises(ValueError, match="does not split"):
        split(1000)
    with pytest.raises(ValueError, match="whole pass"):
        split(1024, width=80)


def test_the_kernel_refuses_a_narrow_sub_row_ITSELF():
    """`split` is advice; the knob is what the kernel runs on, so it checks too."""
    x = np.zeros((4, 8, 80), FP16)
    dev = SimDevice(size=64 << 20)
    with pytest.raises(ValueError, match="whole pass"):
        softmax_wide(dev.tensor(x), rows=8, width=80)


def test_the_fold_is_the_same_tree_over_a_different_monoid():
    """`join` is what lets `group_max` reuse `fold`; the default must not move."""
    import inspect

    assert "join" in inspect.signature(fold).parameters
    assert "join" in inspect.signature(fold_flat).parameters
    assert VLMAX == 128


# --------------------------------------------- the flat call and what it costs
def rows_flat(dev, a, cols):
    """`a` handed over as EVERY row's sub-rows at once, which is the flat call."""
    return dev.tensor(a.reshape(-1, cols // split(cols)))


def flits_of(kern, arrays, **knobs) -> int:
    """Instruction words this compilation emits, addressed by name not by arena."""
    dev = SimDevice(size=1024 << 20)
    compiled = kern.plan(*[dev.tensor(a) for a in arrays], **knobs)
    consts = BACKEND.constants(compiled) or {}
    names = list(compiled.layouts) + list(consts)
    addrs = {n: 0x100000 * (i + 1) for i, n in enumerate(sorted(names))}
    return sum(
        len(w)
        for stage in compiled.stages
        for w in BACKEND.encode(compiled, stage, addrs).values()
    )


@pytest.mark.parametrize("cols", [1024, 4096])
def test_the_flat_call_is_the_SAME_ANSWER_as_the_batched_one(cols):
    """One instance per row was the whole cost, and the answer never depended on it.

    The batched call hands `(m, rows, width)` and pays a program per row; the flat
    call hands `(m*rows, width)` and pays one per `part`.

    NOT bit-identical since reduce/apply grouping: the two shapes group into a
    different number of programs, so an intermediate that survives in an E8M15
    register on one path is rounded to fp16 in DRAM on the other. Both remain
    exactly as accurate, which is what is asserted -- and a fold that walked the
    wrong `vl` would show up here at ~100%, not at one ulp.
    """
    m = 16
    x, w, _ = operands((m, cols))
    rows = split(cols)
    dev = SimDevice(size=1024 << 20)
    batched = rmsnorm_wide(
        rows_of(dev, x, cols), rows_of(dev, w, cols), rows=rows
    ).numpy()
    flatly = rmsnorm_wide(
        rows_flat(dev, x, cols), rows_flat(dev, w, cols), rows=rows
    ).numpy()
    a, b = batched.reshape(m, cols), flatly.reshape(m, cols)
    truth = rms_ref(x, w)
    assert rel(a, b) < 1e-2, "the two shapes are not computing the same function"
    assert abs(rel(a, truth) - rel(b, truth)) < 1e-4, "one shape is less accurate"


@pytest.mark.parametrize("cols,least", [(1024, 5), (4096, 1.6)])
def test_the_flat_call_COSTS_less_by_the_rows_ONE_PART_holds(cols, least):
    """The 19.2x was one grid instance per row, and `part` is what replaces it.

    The saving is `part / group` and so is FLAT in the batch, measured 5.18x at
    1024 and 1.68x at 4096 for m = 16 / 64 / 256 alike. A wider row puts fewer
    rows in a part, which is the whole difference between the two factors here.
    Both were higher before reduce/apply grouping, which helps the BATCHED call
    more: it has the shorter run, so a joined pair is a larger share of it.
    """
    m = 64
    x, w, _ = operands((m, cols))
    rows = split(cols)
    wide = [a.reshape(-1, cols // rows) for a in (x, w)]
    deep = [a.reshape(m, rows, cols // rows) for a in (x, w)]
    assert flits_of(rmsnorm_wide, wide, rows=rows) * least <= flits_of(
        rmsnorm_wide, deep, rows=rows
    )


# ------------------------------- the second half: written before it is read
def unwritten(compiled) -> dict:
    """Temp -> elements no pass of this plan writes. Empty is the only safe answer."""
    per = _per(compiled)
    seen: dict = {}
    for stage in compiled.stages:
        for inst in stage.instances:
            for s in inst.stmts:
                if s.kind not in ("apply", "reduce"):
                    continue
                name = s.args["result"]
                held = _held(compiled, name)
                got = seen.setdefault(name, np.zeros(held, bool))
                at = s.args["part"] * per + s.args["off"]
                # A reduction covers whole rows in one pass; a chain covers the
                # span `_span` clamped it to.
                span = held if s.kind == "reduce" else _span(compiled, s, per)
                got[at : at + span] = True
    return {n: int((~seen[n]).sum()) for n in compiled.temps if n in seen}


@pytest.mark.parametrize("cols", [1024, 4096])
def test_EVERY_fold_temp_is_WRITTEN_before_it_is_read(cols):
    """The flat fold's own correctness bug, and the reason each temp is short.

    Each level's pass is clamped to the shorter of its two shifted operands, so a
    full-length temp keeps a tail nothing wrote -- zero on this model, arbitrary
    on the card, and one Inf there is a NaN through the next level.
    """
    m = 8
    x, w, b = operands((m, cols))
    rows = split(cols)
    wide = [a.reshape(-1, cols // rows) for a in (x, w, b)]
    dev = SimDevice(size=1024 << 20)
    compiled = layernorm_wide.plan(*[dev.tensor(a) for a in wide], rows=rows)
    assert unwritten(compiled) == dict.fromkeys(compiled.temps, 0)


def test_the_last_group_START_is_the_last_sub_row_the_fold_leaves():
    """The boundary the shrinking temps land on, which is tight by one sub-row.

    A fold over `rows` sub-rows loses `rows - 1` of them, so the final buffer is
    `m*rows - (rows - 1)` long and group `m-1` starts at its very last sub-row.
    One shorter and the last row of the batch would read past the end.
    """
    m, rows = 8, 8
    left = m * rows - (rows - 1)
    assert (m - 1) * rows == left - 1
    assert (m - 1) * rows + 1 <= left


# --------------------------------------------------- the periodic read itself
def test_a_spread_is_a_stride_ZERO_dimension_and_nothing_else():
    """`vec_agu.v`: a broadcast IS stride 0. The walk must say so, in bytes."""
    walk = Spread(period=1024, take=128)
    assert walk.repeat == 8
    # One RUN per group needs no group stride; two need one, and it is `period`.
    assert walk.dims(1024) == [(V.WORD_BYTES, 8), (0, 8)]
    assert walk.dims(2048) == [(V.WORD_BYTES, 8), (0, 8), (64 * V.WORD_BYTES, 2)]
    assert [walk.at(1024, b) for b in range(3)] == [0, 1024, 2048]
    assert [Spread(4096, 128).at(1024, b) for b in range(5)] == [0, 0, 0, 0, 4096]


def test_the_walk_visits_exactly_the_words_the_fill_lands():
    """A walk shorter than the fill repeats its last address, silently."""
    for batch, walk in ((1024, Spread(1024, 128)), (1024, Spread(4096, 128))):
        total = 1
        for _, bound in walk.dims(batch):
            total *= bound
        assert total == batch // 16


def test_a_stride_zero_WALK_reads_only_the_sub_row_it_repeats():
    """The probe the design rests on, run against the model rather than argued.

    A wrong bound reads REAL MEMORY and produces plausible numbers, so the check
    has to be positional: the source's unread region is filled with `inf`, and
    none of it may reach a lane. Poison is what an unwritten fold tail looks like
    on the card, where the arena is not zero.
    """
    unit, mem = VectorUnit(mem_base=0), Memory(1 << 22)
    rng = np.random.default_rng(0)
    a = np.asarray(rng.standard_normal(1024), FP16)
    b = np.asarray(rng.standard_normal(1024), FP16)
    b[VLMAX:] = np.inf
    mem.write(0, a.tobytes())
    mem.write(1 << 16, b.tobytes())

    kern = ElementwiseKernel([(OpKind.ADD, [0, 1])], 2)
    walk = Spread(period=1024, take=VLMAX)
    over = [
        V.desc_flit(kern.ad_fill[1], n + 1, V.dim(stride, bound))
        for n, (stride, bound) in enumerate(walk.dims(kern.batch))
    ]
    flits = imem_flits(kern.image) + kern.static_descs() + over
    flits += kern.batch_flits([0, 1 << 16], 1 << 17, 0)
    for flit in flits:
        unit.execute(flit, mem)

    got = np.frombuffer(mem.read(1 << 17, 2048), FP16)
    want = np.asarray(a, np.float64) + np.tile(np.asarray(b[:VLMAX], np.float64), 8)
    assert not np.isinf(got).any(), "the walk read past the sub-row it repeats"
    assert np.array_equal(got, np.asarray(want, FP16))


def test_a_RUN_that_would_cut_a_group_is_refused():
    """A walk starting part way through a period puts every later group wrong."""
    with pytest.raises(VecEmitError, match="whole groups"):
        Spread(period=1024, take=128).dims(1536)
    with pytest.raises(VecEmitError, match="divide the group"):
        Spread(period=4096, take=128).dims(768)


def test_a_band_with_NO_walk_emits_what_it_always_emitted():
    """The additive rule: the feature is invisible to everything not using it."""
    chains = [Chain(((OpKind.ADD, [0, 1]),))]
    plain = BandKernel(chains, 2)
    walked = BandKernel(chains, 2, walks=[None, None])
    assert plain.flits([0, 1 << 16], [1 << 17], 1024) == walked.flits(
        [0, 1 << 16], [1 << 17], 1024
    )


def test_a_spread_RESTORES_the_descriptor_it_widened():
    """A descriptor is CORE STATE: a dim left behind changes the next kernel's walk.

    The next program to reuse the descriptor sets only the first dimension, so a
    second one still holding a bound reads addresses nobody asked for.
    """
    chains = [Chain(((OpKind.ADD, [0, 1]),))]
    walked = BandKernel(chains, 2, walks=[None, Spread(512, 128)])
    out = walked.flits([0, 1 << 16], [1 << 17], 1024)
    reset = [V.desc_flit(walked.ad_fill[1], n, V.dim(0, 1)) for n in (2, 3)]
    assert len(walked._fill_dims(1)) == 3
    assert out[-2:] == reset


@L.kernel
def _same_stage_spread(x=L.In(8, 128), y=L.Out(8, 128), *, rows=8, part=1024):
    """A spread of a buffer written beside it, which must not compile."""
    mu = L.temp(8, 128)
    with units(mu.parts(part)) as e:
        mu[e] <<= x[e] * 1.0
        y[e] <<= x[e] - mu.per_group(rows)[e]


def test_a_spread_is_NEVER_forwarded_from_a_register():
    """What makes the existing race guard see a spread at all.

    A run hands a result forward in a register, and a spread is not in the
    register -- it is in the walk that filled the operand from DRAM. `_leaf_key`
    carries the period, so a periodic read never matches a written region and
    `sequences` refuses to call the pair one program.
    """
    dev = SimDevice(size=64 << 20)
    assert not BACKEND.sequences(*_a_write_then_its_spread())
    with pytest.raises(Exception, match="does not wait between the programs"):
        _same_stage_spread(dev.tensor(np.zeros((8, 128), FP16)))


def _a_write_then_its_spread():
    """The two statements the guard above is about, straight from a trace."""
    dev = SimDevice(size=64 << 20)
    x = np.zeros((8, 128), FP16)
    plan = rmsnorm_wide.plan(dev.tensor(x), dev.tensor(x), rows=8, part=1024)
    stmts = [s for stage in plan.stages for s in _stmts_of(stage)]
    spread = next(
        s
        for s in stmts
        if any(getattr(le, "period", 0) for le in s.args.get("leaves", ()))
    )
    name = next(le.name for le in spread.args["leaves"] if getattr(le, "period", 0))
    return next(s for s in stmts if s.args.get("result") == name), spread


def _stmts_of(stage):
    for inst in stage.instances:
        yield from inst.stmts
