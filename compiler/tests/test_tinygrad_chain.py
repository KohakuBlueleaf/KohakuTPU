"""The elementwise chain generator: what it runs, what it refuses, and the walls.

Three claims. An arbitrary elementwise UOp tree runs as ONE vector program and
agrees with numpy; a tree a library kernel matches is still dispatched to that
kernel, never generated; and everything else RAISES naming the ops it saw.

The ceilings are tested from BOTH sides, because a bound that is merely
conservative is untested. A chain at the generator's own limit must still run on
a vector core, and one past it must be refused HERE rather than at dispatch --
where the emitter's message names the operand count whatever the real cause was.
"""

import numpy as np
import pytest

pytest.importorskip("tinygrad")

import ktpugrad
from kohakutpu.hw.ops import OpKind
from ktpugrad.chain import MAX_HELD, MAX_LEAVES, MAX_OPERANDS, MAX_WORDS
from ktpugrad.generated import chain1, chain2
from tinygrad import Tensor
from tinygrad.device import Device

from kohakutpu import kernels as K
from kohakutpu import ops as O

M, N, KD = 32, 64, 64
WATCH = ("dispatches", "rounds", "flits")


@pytest.fixture(scope="module")
def dev():
    """The KohakuTPU behind the KTPU device, with the seam installed."""
    ktpugrad.install()
    yield Device[ktpugrad.NAME].rt
    ktpugrad.uninstall()


@pytest.fixture
def held():
    """Three result-shaped operands and two per-channel ones."""
    rng = np.random.default_rng(0)
    return [
        (rng.standard_normal(s) * 0.5).astype(np.float16)
        for s in ((M, N), (M, N), (M, N), (N,), (N,))
    ]


def ktensor(a):
    return Tensor(np.ascontiguousarray(a, np.float16), device=ktpugrad.NAME)


def sinks(tensor):
    """The compute kernels `tensor` schedules, without the copies.

    0.13 has no `Tensor.schedule`; the ast exists only where `to_program` is
    handed one, so realizing under a spy is how a test sees it. `strict=False`
    because half the tests here are ABOUT refusals, and the ast is handed over
    before the kernel is chosen.
    """
    return ktpugrad.capture(tensor.realize, strict=False)


def rel(got, want) -> float:
    """Largest absolute error as a fraction of the true peak."""
    want = np.asarray(want, np.float64)
    return float(np.abs(np.asarray(got, np.float64) - want).max() / np.abs(want).max())


def spent(dev, run):
    """`(counters this call moved, its result)`."""
    before = {k: dev.counters[k] for k in WATCH}
    got = run()
    return {k: dev.counters[k] - before[k] for k in WATCH}, got


# ------------------------------------------------------------- what it now runs
CASES = [
    ("add", lambda x, y, z: x + y, lambda x, y, z: x + y),
    ("affine", lambda x, y, z: x * 2 + 1, lambda x, y, z: x * 2 + 1),
    ("scale", lambda x, y, z: x * (1 + y), lambda x, y, z: x * (1 + y)),
    ("modulate", lambda x, y, z: x * (1 + y) + z, lambda x, y, z: x * (1 + y) + z),
    ("sub", lambda x, y, z: x - y, lambda x, y, z: x - y),
    ("div", lambda x, y, z: x / y, lambda x, y, z: x / y),
    ("neg", lambda x, y, z: -x, lambda x, y, z: -x),
    ("exp", lambda x, y, z: x.exp(), lambda x, y, z: np.exp(x)),
    ("recip", lambda x, y, z: x.reciprocal(), lambda x, y, z: 1 / x),
    ("maximum", lambda x, y, z: x.maximum(y), lambda x, y, z: np.maximum(x, y)),
    ("minimum", lambda x, y, z: x.minimum(y), lambda x, y, z: np.minimum(x, y)),
    ("relu", lambda x, y, z: x.relu(), lambda x, y, z: np.maximum(x, 0)),
    ("abs", lambda x, y, z: x.abs(), lambda x, y, z: np.abs(x)),
    ("clip", lambda x, y, z: x.clip(-1, 1), lambda x, y, z: np.clip(x, -1, 1)),
    (
        "relu over a chain",
        lambda x, y, z: (x * 2 + 1).relu(),
        lambda x, y, z: np.maximum(x * 2 + 1, 0),
    ),
    (
        "silu",
        lambda x, y, z: x.silu(),
        lambda x, y, z: x / (1 + np.exp(-x)),
    ),
]


@pytest.mark.parametrize(("name", "build", "want"), CASES, ids=[c[0] for c in CASES])
def test_a_generated_chain_agrees_with_numpy(dev, held, name, build, want):
    """Every one of these was a refusal before the generator existed."""
    x, y, z = held[:3]
    got = build(ktensor(x), ktensor(y), ktensor(z)).numpy()
    wide = [a.astype(np.float64) for a in (x, y, z)]
    assert got.shape == (M, N)
    assert rel(got, want(*wide)) < 5e-3


def test_adalns_own_modulate_runs_with_a_per_channel_scale(dev, held):
    """`norm * (1 + scale) + shift`, which a DiT block writes twice.

    The scale and the shift arrive per-channel, so the frontend spreads them to
    full shape -- an elementwise pass on this machine reads one length.
    """
    x, _, _, scale, shift = held
    got = ktensor(x) * (1 + ktensor(scale)) + ktensor(shift)
    want = x.astype(np.float64) * (1 + scale) + shift
    assert rel(got.numpy(), want) < 5e-3


def test_a_chain_is_ONE_vector_program_not_one_kernel_per_op(dev, held):
    """The whole point: four ops, one dispatch, one stage."""
    x, y, z = held[:3]
    cost, _ = spent(dev, lambda: (ktensor(x) * (1 + ktensor(y)) + ktensor(z)).numpy())
    assert cost["dispatches"] == 1
    made = ktpugrad.read_chain(sinks(ktensor(x) * (1 + ktensor(y)) + ktensor(z))[0])
    kern = ktpugrad.KERNELS[len(made.operands)]
    plan = kern.plan(
        *(Device[ktpugrad.NAME].rt.tensor(a) for a in (x, y, z)), expr=made.root
    )
    assert len(plan.stages) == 1


def test_a_leading_axis_is_carried(dev, held):
    """A chain walks elements, so rank is the tensor level's business."""
    x, y = held[0], held[1]
    a, b = np.stack([x, x]), np.stack([y, y])
    got = (ktensor(a) + ktensor(b)).numpy()
    assert got.shape == (2, M, N)
    assert np.array_equal(got, a + b)


def test_the_same_expression_traces_once(dev, held):
    """The recipe rides as a knob, so a shape rerun is a cache hit."""
    x, y = held[0], held[1]
    made = ktpugrad.read_chain(sinks(ktensor(x) + ktensor(y))[0])
    before = chain2.traces
    rt = Device[ktpugrad.NAME].rt
    for _ in range(3):
        chain2(rt.tensor(x), rt.tensor(y), expr=made.root)
    assert chain2.traces - before <= 1


# ------------------------------------------------- a matched kernel always wins
def test_a_matched_library_kernel_is_never_generated(dev, held):
    """`linear_silu` is hand-tuned and witnessed; the generator must not take it.

    Whether the epilogue ends up fused is the COMPILER's call. What the
    generator must never do is build the chain itself.
    """
    rng = np.random.default_rng(3)
    xa = (rng.standard_normal((M, KD)) * 0.5).astype(np.float16)
    wa = (rng.standard_normal((N, KD)) * 0.5).astype(np.float16)
    ast = sinks((ktensor(xa) @ ktensor(wa).T).silu())[0]
    got = ktpugrad.get_runner(ktpugrad.NAME, ast)
    assert isinstance(got, ktpugrad.LibraryRunner)
    assert got.kernel is K.linear_silu


def test_a_transposed_operand_is_refused(dev, held):
    """A chain walks elements in order; a stride row it does not read is a guess."""
    x = held[0]
    with pytest.raises(ktpugrad.ChainRefused, match="broadcast"):
        ktpugrad.read_chain(sinks(ktensor(x) + ktensor(x.T.copy()).T)[0])


def test_a_whole_buffer_copy_stays_a_copy(dev, held):
    """`Tensor.numpy` schedules a memcpy that WOULD read as an identity chain."""
    x = held[0]
    kept = ktensor(x).contiguous()
    asts = [a for a in sinks(kept) if isinstance(ktpugrad.match_ast(a), ktpugrad.Copy)]
    if not asts:
        pytest.skip("this tinygrad elides the copy")
    assert isinstance(ktpugrad.get_runner(ktpugrad.NAME, asts[0]), ktpugrad.CopyRunner)


def test_the_matcher_is_asked_first(dev, held):
    """A contraction's store is rank-3; the chain reader must refuse it outright."""
    rng = np.random.default_rng(4)
    xa = (rng.standard_normal((M, KD)) * 0.5).astype(np.float16)
    wa = (rng.standard_normal((N, KD)) * 0.5).astype(np.float16)
    ast = sinks(ktensor(xa) @ ktensor(wa).T)[0]
    assert ktpugrad.match_ast(ast).kernel == "matmul"
    with pytest.raises(ktpugrad.ChainRefused):
        ktpugrad.read_chain(ast)


# --------------------------------------- their spelling onto our arithmetic
def test_relu_is_byte_identical_to_the_librarys_own(dev, held):
    """tinygrad writes `WHERE(CMPLT(0, x), x, 0)`; `ops.relu` is `maximum(x, 0)`.

    Two spellings, two lowerings -- a compare into P0 with predicated moves, and
    one `VMAX` word -- and they must still agree to the bit.
    """
    x = held[0]
    rt = Device[ktpugrad.NAME].rt
    made = ktpugrad.read_chain(sinks(ktensor(x).relu())[0])
    got = chain1(rt.tensor(x), expr=made.root).numpy()
    assert np.array_equal(got, O.relu(rt.tensor(x)).numpy())


def test_abs_is_one_VABS_and_not_a_select_tree(dev, held):
    """`x * sign(x)` is five UOps and three of them are predication; ours is one."""
    x = held[0]
    made = ktpugrad.read_chain(sinks(ktensor(x).abs())[0])
    assert [n.kind for n in made.nodes] == [OpKind.ABS]
    assert made.words == 1


# ------------------------------------------------------------ what it refuses
REFUSALS = [
    # THREE operands, so it is a real predicate. `(x < y).where(x, y)` is not:
    # it is `min(x, y)`, and `_minmax_of` reads it.
    ("select", lambda x, y: (x < y).where(x + y, y), "WHERE"),
    ("sign", lambda x, y: x.sign(), "CMPNE"),
    ("down a column", lambda x, y: x.sum(axis=0), "only the last axis"),
    ("two axes", lambda x, y: x.sum(axis=(0, 1)), "one SINK of one stored loop nest"),
    ("a product", lambda x, y: x.prod(axis=1), "MUL reduction"),
    ("fp32", lambda x, y: x.float() + y.float(), "fp16"),
]


@pytest.mark.parametrize(
    ("name", "build", "says"), REFUSALS, ids=[r[0] for r in REFUSALS]
)
def test_an_unspellable_tree_raises_and_names_itself(dev, held, name, build, says):
    """Guessing an operator computes a different function and reports success."""
    x, y = held[0], held[1]
    with pytest.raises(ktpugrad.KTPUUnsupported, match=says):
        build(ktensor(x), ktensor(y)).numpy()


def test_predication_in_general_is_still_the_boundary(dev, held):
    """The min/max shapes are rewritten; a select that is neither is refused."""
    x, y = held[0], held[1]
    why = ktpugrad.refusal(
        sinks((ktensor(x) < ktensor(y)).where(ktensor(x) + ktensor(y), ktensor(y)))[0]
    )
    assert "CMPLT" in why and "WHERE" in why


def test_a_min_or_max_written_as_a_select_is_read(dev, held):
    """`clip` is two of these nested, so refusing them refused `clip`."""
    x, y = held[0], held[1]
    wide = [a.astype(np.float64) for a in (x, y)]
    for build, want in (
        (lambda a, b: (a < b).where(a, b), np.minimum),
        (lambda a, b: (a < b).where(b, a), np.maximum),
    ):
        got = build(ktensor(x), ktensor(y)).numpy()
        assert rel(got, want(*wide)) < 5e-3


def test_a_refusal_names_every_op_it_could_not_spell(dev, held):
    """The first unspellable op is not the whole story; `sign` holds three."""
    x = held[0]
    why = ktpugrad.refusal(sinks(ktensor(x).sign())[0])
    assert "CMPLT" in why and "CMPNE" in why and "WHERE" in why


def test_a_refusal_is_never_a_fallback(dev, held):
    """Computing on the host and reporting device numbers is the failure mode."""
    x = held[0]
    with pytest.raises(ktpugrad.ChainRefused):
        ktensor(x).sign().numpy()


def test_a_fourth_operand_is_refused(dev, held):
    """Two descriptors per operand, one shared store and one drain, against eight."""
    a, b, c, *_ = held
    d = np.ones((M, N), np.float16)
    built = ktensor(a) + ktensor(b) + ktensor(c) + ktensor(d)
    with pytest.raises(ktpugrad.ChainRefused, match="operands"):
        ktpugrad.read_chain(sinks(built)[0])


# --------------------------------------------------------------- the ceilings
def test_the_ceilings_are_the_machines_own():
    """Each is read off the emitter, not chosen: a copy here would drift."""
    assert (MAX_OPERANDS, MAX_LEAVES, MAX_HELD) == (3, 7, 11)
    assert MAX_WORDS == 57


def test_a_chain_at_the_word_ceiling_still_runs(dev, held):
    """A bound below the emitter's real one, pinned from the emitter's side."""
    rt = Device[ktpugrad.NAME].rt
    node = ktpugrad.Load(0)
    for _ in range(MAX_WORDS):
        node = ktpugrad.Op(OpKind.RECIP, (node,))
    assert chain1(rt.tensor(held[0]), expr=node).numpy().shape == (M, N)


def test_a_chain_past_the_word_ceiling_is_refused_here_not_at_dispatch(dev, held):
    """A band UNROLLS every step, so 62 unary ops built a 520-word image.

    Left to the emitter this raises at DISPATCH with a message naming the
    operand count, whatever the real cause was.
    """
    x, y = held[0], held[1]
    a, b = ktensor(x), ktensor(y)
    v = a
    for _ in range(30):
        v = (v + b) * 0.5
    with pytest.raises(ktpugrad.ChainRefused, match="instruction words"):
        ktpugrad.read_chain(sinks(v)[0])


def test_the_recipe_counts_a_shared_subexpression_once(dev, held):
    """`chains_of` lifts it into one chain the others read from a register."""
    x = held[0]
    made = ktpugrad.read_chain(sinks(ktensor(x).silu())[0])
    assert made.words == len(made.nodes)
    assert made.held >= 1


# ------------------------------------------------- a REDUCE_AXIS inside a chain
FOLDS = [
    ("sum", lambda x, y: x.sum(axis=-1), lambda x, y: x.sum(1)),
    ("max", lambda x, y: x.max(axis=-1), lambda x, y: x.max(1)),
    ("min", lambda x, y: x.min(axis=-1), lambda x, y: x.min(1)),
    ("mean", lambda x, y: x.mean(axis=-1), lambda x, y: x.mean(1)),
    ("sum of squares", lambda x, y: (x * x).sum(axis=-1), lambda x, y: (x * x).sum(1)),
    ("a dot product", lambda x, y: (x * y).sum(axis=-1), lambda x, y: (x * y).sum(1)),
    (
        "under the fold",
        lambda x, y: x.exp().sum(axis=-1),
        lambda x, y: np.exp(x).sum(1),
    ),
    ("over the fold", lambda x, y: x.sum(axis=-1) * 2, lambda x, y: x.sum(1) * 2),
    (
        "relu then fold",
        lambda x, y: x.relu().sum(-1),
        lambda x, y: np.maximum(x, 0).sum(1),
    ),
    ("abs then fold", lambda x, y: x.abs().max(-1), lambda x, y: np.abs(x).max(1)),
]


@pytest.mark.parametrize(("name", "build", "want"), FOLDS, ids=[f[0] for f in FOLDS])
def test_a_generated_fold_agrees_with_numpy(dev, held, name, build, want):
    """The trap is that a wrong `vl` folds TWO rows and reports success.

    Nothing structural catches that -- it is a legal program computing a
    different function -- so every one of these is graded against numpy.
    """
    x, y = held[0], held[1]
    got = build(ktensor(x), ktensor(y)).numpy()
    assert got.shape == (M,)
    assert rel(got, want(x.astype(np.float64), y.astype(np.float64))) < 5e-3


def test_a_fold_walks_the_ROW_and_the_recipe_says_so(dev, held):
    """`shape` is what the program walks, `stored` what tinygrad's buffer holds.

    0.13 drops the reduced axis rather than leaving it at one, so `stored` is
    `(M,)` where a ShapeTracker gave `(M, 1)`. Same bytes either way.
    """
    x = held[0]
    made = ktpugrad.read_chain(sinks(ktensor(x).sum(axis=-1))[0])
    assert made.folds
    assert (made.shape, made.stored) == ((M, N), (M,))
    assert [n.kind for n in made.nodes] == [OpKind.SUM]


def test_a_folding_chain_takes_WHOLE_ROWS_per_instance(dev, held):
    """A band steps one ROW when it reduces, so an instance needs whole rows.

    It does NOT need the whole array: a row is folded by a single step, so
    which instance takes it cannot change the answer. Half the array is 16
    whole rows and grids; a part that cuts a row still folds two into one and
    is still refused.
    """
    x = held[0]
    t = ktensor(x)
    made = ktpugrad.read_chain(sinks((t * t).sum(axis=-1))[0])
    rt = Device[ktpugrad.NAME].rt
    kern = ktpugrad.FOLDS[len(made.operands)]
    assert len(kern.plan(rt.tensor(x), expr=made.root, part=M * N).stages) == 1
    assert len(kern.plan(rt.tensor(x), expr=made.root, part=M * N // 2).stages) == 1
    with pytest.raises(Exception, match="folds rows"):
        kern(rt.tensor(x), expr=made.root, part=N // 2)


def test_a_fold_is_ONE_dispatch(dev, held):
    """Reduce and epilogue in one program, not a pass each."""
    x = held[0]
    cost, _ = spent(dev, lambda: (ktensor(x) * ktensor(x)).sum(axis=-1).numpy())
    assert cost["dispatches"] == 1


def test_softmax_now_GENERATES_and_still_costs_three_dispatches(dev, held):
    """What this landing buys is RUNNING whole, not FUSING whole.

    tinygrad never fuses a REDUCE_AXIS with a full-shape consumer -- every
    reduce-bearing sink stores `[rows][1]` and the consumer reads it back in a
    kernel of its own. So the escape hatch is still the faster answer.
    """
    x = held[0]
    assert all(ktpugrad.read_chain(a) for a in sinks(ktensor(x).softmax()))
    cost, got = spent(dev, lambda: ktensor(x).softmax().numpy())
    assert cost["dispatches"] == 3
    wide = x.astype(np.float64)
    ex = np.exp(wide - wide.max(1, keepdims=True))
    assert rel(got, ex / ex.sum(1, keepdims=True)) < 5e-3


REFUSED_FOLDS = [
    ("down a column", lambda x: x.sum(axis=0), "only the last axis"),
    (
        "two axes at once",
        lambda x: x.sum(axis=(0, 1)),
        "one SINK of one stored loop nest",
    ),
    ("a product", lambda x: x.prod(axis=-1), "MUL reduction"),
]


@pytest.mark.parametrize(
    ("name", "build", "says"), REFUSED_FOLDS, ids=[r[0] for r in REFUSED_FOLDS]
)
def test_a_fold_this_machine_cannot_walk_is_refused(dev, held, name, build, says):
    """A fold over the wrong extent is a legal program with a wrong answer."""
    with pytest.raises(ktpugrad.ChainRefused, match=says):
        ktpugrad.read_chain(sinks(build(ktensor(held[0])))[0])


def test_a_batched_operand_is_refused_rather_than_flattened(dev):
    """`rows_cols` reads `(trailing[0], trailing[-1])`, which for [B][M][N] is (B, N).

    That is the WRONG row count, and the only thing between it and a wrong `vl`
    is a span check downstream. Refused here so the guard is this file's own.
    """
    held = np.zeros((2, M, N), np.float16)
    with pytest.raises(ktpugrad.ChainRefused, match="rank-3"):
        ktpugrad.read_chain(sinks(ktensor(held).sum(axis=-1))[0])


@pytest.mark.parametrize("cols", [24, 1024])
def test_a_row_VRED_cannot_fold_is_refused(dev, cols):
    """A multiple of 16 up to VLMAX, which is 128 -- so a DiT's L=1024 refuses."""
    held = np.zeros((8, cols), np.float16)
    with pytest.raises(ktpugrad.ChainRefused, match="wide row"):
        ktpugrad.read_chain(sinks(ktensor(held).sum(axis=-1))[0])


@pytest.mark.parametrize("cols", [256, 1024])
def test_the_hatch_is_STILL_not_the_way_round_a_wide_row(dev, cols):
    """The wall that remains after `kernels.wide`: neither L5 rung reshapes.

    A refusal naming an alternative is worth more than the feature it refuses --
    and worth LESS than nothing if the alternative is shut too. The wide kernels
    take the row SPLIT, so reaching them is a shape change the hatch does not
    make, and this pins that the hatch itself still refuses.
    """
    held = ktensor(np.zeros((8, cols), np.float16))
    why = ktpugrad.refusal(sinks(held.sum(axis=-1))[0])
    assert "not the way round" in why
    assert "layernorm_wide" in why
    with pytest.raises(Exception, match="VRED needs a multiple"):
        ktpugrad.softmax(held)


def test_the_hierarchical_form_the_refusal_names_does_exist(dev):
    """The door the message names, on the same runtime the hatch refused on."""
    from kohakutpu.kernels import softmax_wide, split

    rows, width, cols = split(1024), 128, 1024
    x = (np.random.default_rng(0).standard_normal((4, cols)) * 0.5).astype(np.float16)
    rt = Device[ktpugrad.NAME].rt
    got = softmax_wide(rt.tensor(x.reshape(4, rows, width)), rows=rows).numpy()
    wide = np.exp(np.float64(x) - np.float64(x).max(-1, keepdims=True))
    assert rel(got.reshape(4, cols), wide / wide.sum(-1, keepdims=True)) < 5e-3


# ---------------------------------------------- rsqrt, and the composed root
def test_rsqrt_is_read_as_the_PAIR_and_never_through_sqrt(dev, held):
    """tinygrad writes `RECIP(SQRT(x))`; ours is one `VRSQRT`, not two words.

    The fold has to be matched BEFORE the generic walk or `rsqrt` would cost
    `_sqrt`'s pair plus a `VINV` -- three words and three table errors for
    something this ALU does in one.
    """
    x = np.abs(held[0]) + 0.5
    made = ktpugrad.read_chain(sinks(ktensor(x).rsqrt())[0])
    assert [n.kind for n in made.nodes] == [OpKind.RSQRT]
    assert made.words == 1
    got = ktensor(x).rsqrt().numpy()
    assert rel(got, 1 / np.sqrt(x.astype(np.float64))) < 5e-3


def test_a_bare_sqrt_is_the_two_word_pair_of_seeds(dev, held):
    """`x.sqrt()` runs as `1/(1/sqrt(x))` -- `L.sqrt_approx`, not the Newton form.

    MEASURED at 1.556 ulp of E8M15 against the Newton path's 1.467, for a third
    of the words (`scripts/py/sqrt_paths.py`). The agreement below is against
    the MODEL, which computes both seeds exactly, so it pins the algebra and
    says nothing about the silicon -- that is what the script is for.
    """
    x = np.abs(held[0]) + 0.5
    made = ktpugrad.read_chain(sinks(ktensor(x).sqrt())[0])
    assert [n.kind for n in made.nodes] == [OpKind.RECIP, OpKind.RSQRT]
    assert made.words == 2
    got = ktensor(x).sqrt().numpy()
    assert rel(got, np.sqrt(x.astype(np.float64))) < 5e-3


NORMS = [
    (
        "softmax",
        3,
        lambda t: t.softmax(),
        lambda x: (
            np.exp(x - x.max(1, keepdims=True))
            / np.exp(x - x.max(1, keepdims=True)).sum(1, keepdims=True)
        ),
    ),
    (
        "rmsnorm",
        2,
        lambda t: t * (t.square().mean(-1, keepdim=True) + 1e-5).rsqrt(),
        lambda x: x / np.sqrt((x * x).mean(1, keepdims=True) + 1e-5),
    ),
    (
        "layernorm",
        3,
        lambda t: t.layernorm(),
        lambda x: (
            (x - x.mean(1, keepdims=True)) / np.sqrt(x.var(1, keepdims=True) + 1e-5)
        ),
    ),
]


@pytest.mark.parametrize(
    ("name", "kernels", "build", "want"), NORMS, ids=[n[0] for n in NORMS]
)
def test_the_row_wise_kernels_GENERATE_WHOLE(dev, held, name, kernels, build, want):
    """What the reduction plus the rsqrt pair unlock, and what they do NOT.

    Every sink generates, so none of these needs the escape hatch to RUN. The
    kernel count does not move: tinygrad gives a REDUCE_AXIS its own kernel and
    the consumer reads the result back, so fusing them is still above this seam.
    """
    x = held[0]
    asts = sinks(build(ktensor(x)))
    assert len(asts) == kernels
    assert all(ktpugrad.read_chain(a) for a in asts)
    cost, got = spent(dev, lambda: build(ktensor(x)).numpy())
    assert cost["dispatches"] == kernels
    assert rel(got, want(x.astype(np.float64))) < 5e-3
