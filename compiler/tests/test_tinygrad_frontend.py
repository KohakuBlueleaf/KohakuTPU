"""tinygrad as a frontend: the round trip, the two matches, and the refusals.

Two claims and one boundary. A tensor put on the device must come back
unchanged; an expression the matcher recognises must return the SAME bytes as
calling the library kernel by hand; and everything else must be refused by name
rather than computed some other way.

The refusals are asserted as tightly as the matches. A matcher that quietly
widened would dispatch a wrong kernel and report success, and the ones recorded
here -- softmax, a batch, fp32 -- are the shapes `.plan/TINYGRAD.md` predicted
would not fit.

The ESCAPE HATCH is held to the same line: `ktpugrad.softmax` and its three
siblings run, and a refused ast still RAISES, so the hatch is never reached by
falling back into it.
"""

import gc

import numpy as np
import pytest

pytest.importorskip("tinygrad")

import ktpugrad
from tinygrad import Tensor
from tinygrad.device import Device

from kohakutpu import kernels as K
from kohakutpu import ops as O

M, N, KD = 32, 64, 64
WATCH = ("dispatches", "rounds", "flits")


@pytest.fixture(scope="module")
def dev():
    """The KohakuTPU behind the KTPU device, with the seam installed.

    Module-scoped: `Device[...]` caches its `Compiled`, so one machine serves
    the file whatever this does.
    """
    ktpugrad.install()
    yield Device[ktpugrad.NAME].rt
    ktpugrad.uninstall()


@pytest.fixture
def operands():
    """`(x, w)` as host arrays: `x` is `[M][K]` and `w` is `[N][K]`."""
    rng = np.random.default_rng(0)
    return (
        (rng.standard_normal((M, KD)) * 0.5).astype(np.float16),
        (rng.standard_normal((N, KD)) * 0.5).astype(np.float16),
    )


@pytest.fixture
def bias():
    """A per-column bias, `[N]`, which is what a torch Linear keeps."""
    return (np.random.default_rng(1).standard_normal(N) * 0.5).astype(np.float16)


def sinks(tensor):
    """The compute kernels `tensor` schedules, without the copies.

    0.13 has no `Tensor.schedule`; the ast exists only where `to_program` is
    handed one, so realizing under a spy is how a test sees it.
    """
    return ktpugrad.capture(tensor.realize)


def spent(dev, run):
    """`(counters this call moved, its result)`."""
    before = {k: dev.counters[k] for k in WATCH}
    got = run()
    return {k: dev.counters[k] - before[k] for k in WATCH}, got


# ------------------------------------------------------------- the round trip
def test_a_tensor_round_trips_through_the_arena(dev):
    """Step 1: the allocator alone, with no kernel anywhere in it."""
    held = (np.arange(M * KD).reshape(M, KD) % 7).astype(np.float16)
    assert np.array_equal(Tensor(held, device=ktpugrad.NAME).numpy(), held)


def test_a_tensor_copied_onto_the_device_round_trips(dev):
    """`.to(...)` rather than `device=`, which crosses two devices."""
    held = (np.arange(M * KD).reshape(M, KD) % 5).astype(np.float16)
    assert np.array_equal(Tensor(held).to(ktpugrad.NAME).numpy(), held)


def test_a_buffer_of_an_odd_length_round_trips(dev):
    """Every transport takes whole 64-bit words; 7 fp16 is not one."""
    held = np.arange(7, dtype=np.float16)
    assert np.array_equal(Tensor(held, device=ktpugrad.NAME).numpy(), held)


def test_a_dead_buffer_goes_back_to_the_arena(dev):
    """A frontend that leaks the arena is a frontend nothing can run twice."""
    held = np.ones((M, KD), np.float16)
    live = dev.arena.stats()["live"]
    kept = Tensor(held, device=ktpugrad.NAME).realize()
    assert dev.arena.stats()["live"] > live
    del kept
    gc.collect()
    assert dev.arena.stats()["live"] == live


# ------------------------------------------------------------------ the match
@pytest.mark.parametrize("order", ["NK", "KN"])
def test_a_matmul_is_one_kernel_whichever_way_the_weight_is_stored(
    dev, operands, order
):
    """`x @ w.T` gives the library's own `[N][K]`; `x @ w` gives `[K][N]`."""
    xa, wa = operands
    x = Tensor(xa, device=ktpugrad.NAME)
    w = Tensor(wa if order == "NK" else wa.T.copy(), device=ktpugrad.NAME)
    asts = sinks(x @ w.T if order == "NK" else x @ w)
    assert len(asts) == 1
    got = ktpugrad.match_ast(asts[0])
    assert isinstance(got, ktpugrad.Match)
    assert (got.kernel, got.b.order, got.a.order) == ("matmul", order, "MK")
    assert (got.m, got.k, got.n) == (M, KD, N)


@pytest.mark.parametrize("order", ["NK", "KN"])
def test_a_matmul_returns_the_librarys_own_bytes(dev, operands, order):
    """Byte identity, not tolerance: the frontend must ADD no arithmetic."""
    xa, wa = operands
    x = Tensor(xa, device=ktpugrad.NAME)
    w = Tensor(wa if order == "NK" else wa.T.copy(), device=ktpugrad.NAME)
    got = (x @ w.T if order == "NK" else x @ w).numpy()
    assert np.array_equal(got, O.matmul(dev.tensor(xa), dev.tensor(wa)).numpy())


def test_a_matmul_agrees_with_numpy(dev, operands):
    """The library's own MXFP7 error and no more."""
    xa, wa = operands
    got = (
        Tensor(xa, device=ktpugrad.NAME) @ Tensor(wa, device=ktpugrad.NAME).T
    ).numpy()
    want = xa.astype(np.float32) @ wa.astype(np.float32).T
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


# --------------------------------------------------------------- the epilogue
def test_the_silu_epilogue_is_one_kernel(dev, operands):
    """The deciding case: tinygrad's fusion is the fusion this machine wants."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    asts = sinks((x @ w.T).silu())
    assert len(asts) == 1
    got = ktpugrad.match_ast(asts[0])
    assert isinstance(got, ktpugrad.Match) and got.kernel == "linear_silu"


def picked(dev, m, k, n):
    """The kernel and knobs `plan` prices cheapest here, which is what runs."""
    return ktpugrad.plan(
        K.linear_silu, m, n, dev.machine.count("VC"), k=k, machine=dev.machine
    )


def test_the_silu_epilogue_returns_the_librarys_own_bytes(dev, operands):
    """Against whichever twin `plan` picks: since it PRICES them, the cheaper
    one is the library's own program just as much as the fused one was."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = (x @ w.T).silu().numpy()
    kern, knobs = picked(dev, *xa.shape, wa.shape[0])
    want = kern(dev.tensor(xa), dev.tensor(wa), **knobs).numpy()
    assert np.array_equal(got, want)


def test_the_epilogue_dispatches_the_librarys_program(dev, operands):
    """Same rounds and same flits as the library call, or the fusion is a claim.

    A frontend can agree element for element and still have run the unfused
    pair; what says it did not is the program it dispatched. `plan` prices
    `linear_silu_separated` cheaper at this shape -- 2 rounds / 112 flits
    against 3 / 150 -- so that is what both sides of this comparison run.
    """
    xa, wa = operands
    kern, knobs = picked(dev, *xa.shape, wa.shape[0])
    theirs, _ = spent(
        dev, lambda: kern(dev.tensor(xa), dev.tensor(wa), **knobs).numpy()
    )
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    through, _ = spent(dev, lambda: (x @ w.T).silu().numpy())
    assert through == theirs


def test_there_is_ONE_kernel_to_return_now_that_the_twin_is_gone(dev, operands):
    """`plan` used to weigh a `*_separated` twin and pick the cheaper.

    Those kernels are gone -- the COMPILER stages a fusion it cannot perform --
    so there is no second kernel to weigh and `plan` only picks a tiling.
    Restoring the choice needs `fuse` declared as a knob; see `tiling.plan`.
    """
    xa, wa = operands
    m, k, n = *xa.shape, wa.shape[0]
    cores = dev.machine.count("VC")
    assert ktpugrad.plan(K.linear_silu, m, n, cores)[0] is K.linear_silu
    assert picked(dev, m, k, n)[0] is K.linear_silu


# ---------------------------------------------------------------------- relu
@pytest.mark.parametrize(
    ("name", "build"),
    [("relu", lambda t: t.relu()), ("maximum", lambda t: t.maximum(0))],
)
def test_relu_is_one_kernel_whichever_way_it_is_written(dev, operands, name, build):
    """`WHERE(CMPLT)` and `MAX` are tinygrad's two spellings of the same clamp."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    asts = sinks(build(x @ w.T))
    assert len(asts) == 1
    got = ktpugrad.match_ast(asts[0])
    assert isinstance(got, ktpugrad.Match) and got.kernel == "linear_relu"


def test_relu_returns_the_librarys_own_bytes(dev, operands):
    """Their spelling, our arithmetic: `(x + |x|) * 0.5` and nothing else."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = (x @ w.T).relu().numpy()
    assert np.array_equal(got, K.linear_relu(dev.tensor(xa), dev.tensor(wa)).numpy())


def test_relu_agrees_with_numpy(dev, operands):
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = (x @ w.T).relu().numpy()
    want = np.maximum(xa.astype(np.float32) @ wa.astype(np.float32).T, 0)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


# ---------------------------------------------------------------------- bias
@pytest.mark.parametrize("order", ["N", "MN"])
def test_a_bias_add_is_one_kernel_however_it_broadcasts(dev, operands, bias, order):
    """A `[N]` bias and a full-shape residual are the same fused ADD."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    held = bias if order == "N" else np.broadcast_to(bias, (M, N)).copy()
    asts = sinks((x @ w.T) + Tensor(held, device=ktpugrad.NAME))
    assert len(asts) == 1
    got = ktpugrad.match_ast(asts[0])
    assert isinstance(got, ktpugrad.Match) and got.kernel == "linear_add"
    assert got.c is not None and got.c.order == order


def test_a_bias_add_returns_the_librarys_own_bytes(dev, operands, bias):
    """The frontend spreads the bias; the kernel is `linear_add` either way."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = ((x @ w.T) + Tensor(bias, device=ktpugrad.NAME)).numpy()
    spread = dev.tensor(np.broadcast_to(bias, (M, N)))
    want = K.linear_add(dev.tensor(xa), dev.tensor(wa), spread).numpy()
    assert np.array_equal(got, want)


def test_a_bias_add_agrees_with_numpy(dev, operands, bias):
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = ((x @ w.T) + Tensor(bias, device=ktpugrad.NAME)).numpy()
    want = xa.astype(np.float32) @ wa.astype(np.float32).T + bias.astype(np.float32)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


def test_a_column_bias_is_matched_too(dev, operands):
    """`(M, 1)` spreads across a row, which is a third stride row and no more."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    col = (np.arange(M, dtype=np.float16) * 0.01).reshape(M, 1)
    got = ktpugrad.match_ast(sinks((x @ w.T) + Tensor(col, device=ktpugrad.NAME))[0])
    assert isinstance(got, ktpugrad.Match) and got.c.order == "M"


# ------------------------------------------------------- the MLP layer itself
@pytest.mark.parametrize(
    ("name", "build"),
    [("relu", lambda t: t.relu()), ("maximum", lambda t: t.maximum(0))],
)
@pytest.mark.parametrize("order", ["N", "MN"])
def test_a_bias_then_a_clamp_is_one_kernel(dev, operands, bias, name, build, order):
    """`((x @ w.T) + b).relu()` -- the commonest layer in an MLP, as ONE ast.

    The clamp's own operand is the ADD that `linear_add` matches, so a matcher
    reading the ADD first would dispatch the bias alone and drop the relu.
    """
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    held = bias if order == "N" else np.broadcast_to(bias, (M, N)).copy()
    asts = sinks(build((x @ w.T) + Tensor(held, device=ktpugrad.NAME)))
    assert len(asts) == 1
    got = ktpugrad.match_ast(asts[0])
    assert isinstance(got, ktpugrad.Match) and got.kernel == "linear_add_relu"
    assert got.c is not None and got.c.order == order


def test_a_bias_then_a_clamp_returns_the_librarys_own_bytes(dev, operands, bias):
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = ((x @ w.T) + Tensor(bias, device=ktpugrad.NAME)).relu().numpy()
    spread = dev.tensor(np.broadcast_to(bias, (M, N)))
    want = K.linear_add_relu(dev.tensor(xa), dev.tensor(wa), spread).numpy()
    assert np.array_equal(got, want)


def test_a_bias_then_a_clamp_dispatches_the_fused_program(dev, operands, bias):
    """Same rounds and flits as the library kernel, or the fusion is a claim."""
    xa, wa = operands
    spread = np.broadcast_to(bias, (M, N))
    fused, _ = spent(
        dev,
        lambda: K.linear_add_relu(
            dev.tensor(xa), dev.tensor(wa), dev.tensor(spread)
        ).numpy(),
    )
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    b = Tensor(bias, device=ktpugrad.NAME)
    through, _ = spent(dev, lambda: ((x @ w.T) + b).relu().numpy())
    assert through == fused


def test_a_bias_then_a_clamp_agrees_with_numpy(dev, operands, bias):
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = ((x @ w.T) + Tensor(bias, device=ktpugrad.NAME)).relu().numpy()
    acc = xa.astype(np.float32) @ wa.astype(np.float32).T + bias.astype(np.float32)
    want = np.maximum(acc, 0)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


# --------------------------------------------------- the rest of a DiT block
def np_gelu(v):
    """The tanh GELU in fp32, which is what `.gelu()` means."""
    return 0.5 * v * (1 + np.tanh(np.sqrt(2 / np.pi) * (v + 0.044715 * v**3)))


def np_silu(v):
    return v / (1 + np.exp(-v))


#: `(name, the tinygrad expression, the numpy answer)`. `acc` is the plain
#: contraction, `b` a `[N]` bias, `g` a `[N]` gate and `r` an `[M][N]` residual.
BLOCK = [
    ("linear_gelu", lambda t: t.acc.gelu(), lambda n: np_gelu(n.acc)),
    ("linear_add_gelu", lambda t: (t.acc + t.b).gelu(), lambda n: np_gelu(n.acc + n.b)),
    ("linear_add_silu", lambda t: (t.acc + t.b).silu(), lambda n: np_silu(n.acc + n.b)),
    ("linear_scale", lambda t: t.acc * 0.125, lambda n: n.acc * 0.125),
    ("linear_gate_add", lambda t: t.r + t.g * t.acc, lambda n: n.r + n.g * n.acc),
]


class Held:
    """The operands of one expression, as tinygrad Tensors or as numpy."""

    def __init__(self, xa, wa, extra, tensors: bool) -> None:
        make = (lambda a: Tensor(a, device=ktpugrad.NAME)) if tensors else np.float32
        self.acc = (
            make(xa) @ make(wa).T
            if tensors
            else xa.astype(np.float32) @ wa.astype(np.float32).T
        )
        for name, held in extra.items():
            setattr(self, name, make(held))


@pytest.fixture
def block(operands, bias):
    """`(as tinygrad, as numpy)` builders for the DiT-block operands."""
    xa, wa = operands
    gate = (np.random.default_rng(2).standard_normal(N) * 0.5).astype(np.float16)
    res = (np.random.default_rng(3).standard_normal((M, N)) * 0.5).astype(np.float16)
    extra = {"b": bias, "g": gate, "r": res}
    return (
        lambda: Held(xa, wa, extra, True),
        lambda: Held(xa, wa, extra, False),
        extra,
    )


@pytest.mark.parametrize(
    ("name", "build", "want"), BLOCK, ids=[row[0] for row in BLOCK]
)
def test_a_dit_block_expression_is_one_kernel(dev, block, name, build, want):
    """Each is ONE tinygrad kernel and each names the kernel written for it."""
    tensors, _, _ = block
    asts = sinks(build(tensors()))
    assert len(asts) == 1
    got = ktpugrad.match_ast(asts[0])
    assert isinstance(got, ktpugrad.Match) and got.kernel == name


@pytest.mark.parametrize(
    ("name", "build", "want"), BLOCK, ids=[row[0] for row in BLOCK]
)
def test_a_dit_block_expression_agrees_with_numpy(dev, block, name, build, want):
    """The library's own MXFP7 error and nothing the frontend added."""
    tensors, arrays, _ = block
    got = build(tensors()).numpy()
    ref = want(arrays())
    assert np.abs(got - ref).max() / np.abs(ref).max() < 0.05


def test_the_scale_reaches_the_kernel_as_a_knob(dev, block):
    """`(x @ w.T) * s` folds `s` into the epilogue; a dropped one is silent.

    Two scales one octave apart, so fp16 scales exactly: a matcher that read the
    constant and then failed to pass it on returns the same bytes twice.
    """
    tensors, _, _ = block
    assert ktpugrad.match_ast(sinks(tensors().acc * 0.125)[0]).scale == 0.125
    eighth = (tensors().acc * 0.125).numpy().astype(np.float32)
    quarter = (tensors().acc * 0.25).numpy().astype(np.float32)
    assert np.abs(eighth).max() > 0
    assert np.array_equal(2 * eighth, quarter)


# ------------------------------------------------------------- the escape hatch
ROWS = 64


@pytest.fixture
def rowwise():
    """`(x, gain, shift)` as fp16 host arrays, square so a row is a reduction."""
    rng = np.random.default_rng(7)
    return (
        (rng.standard_normal((ROWS, N)) * 0.5).astype(np.float16),
        (rng.standard_normal(N) * 0.5).astype(np.float16),
        (rng.standard_normal(N) * 0.5).astype(np.float16),
    )


@pytest.fixture
def heads():
    """`(q, k, v)` as fp16 host arrays, one 64-wide block each."""
    rng = np.random.default_rng(8)
    return tuple(
        (rng.standard_normal((ROWS, N)) * 0.5).astype(np.float16) for _ in range(3)
    )


def ktensor(held):
    """A host array as a fp16 tinygrad Tensor on the KTPU device."""
    return Tensor(np.ascontiguousarray(held, np.float16), device=ktpugrad.NAME)


def test_the_hatch_computes_softmax_the_scheduler_split(dev, rowwise):
    """Three tinygrad kernels, none of which match; one of ours, which runs."""
    xa, _, _ = rowwise
    assert len(sinks(ktensor(xa).softmax())) == 3
    got = ktpugrad.softmax(ktensor(xa)).numpy()
    held = xa.astype(np.float32)
    shifted = held - held.max(-1, keepdims=True)
    want = np.exp(shifted) / np.exp(shifted).sum(-1, keepdims=True)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.01


def test_the_hatch_computes_rmsnorm(dev, rowwise):
    xa, ga, _ = rowwise
    got = ktpugrad.rmsnorm(ktensor(xa), ktensor(ga)).numpy()
    held, gain = xa.astype(np.float32), ga.astype(np.float32)
    want = held / np.sqrt((held**2).mean(-1, keepdims=True) + 1e-5) * gain
    assert np.abs(got - want).max() / np.abs(want).max() < 0.01


def test_the_hatch_computes_layernorm(dev, rowwise):
    xa, ga, ba = rowwise
    got = ktpugrad.layernorm(ktensor(xa), ktensor(ga), ktensor(ba)).numpy()
    held = xa.astype(np.float32)
    mu = held.mean(-1, keepdims=True)
    var = ((held - mu) ** 2).mean(-1, keepdims=True)
    want = (held - mu) / np.sqrt(var + 1e-5) * ga.astype(np.float32) + ba.astype(
        np.float32
    )
    assert np.abs(got - want).max() / np.abs(want).max() < 0.01


@pytest.mark.parametrize("causal", [False, True])
def test_the_hatch_computes_attention(dev, heads, causal):
    """The one that decides it: `scaled_dot_product_attention` matches NOTHING.

    Five tinygrad kernels and zero matches, so attention does not run on this
    rung at all without the hatch.
    """
    qa, ka, va = heads
    got = ktpugrad.attention(
        ktensor(qa), ktensor(ka), ktensor(va), causal=causal
    ).numpy()
    scores = qa.astype(np.float32) @ ka.astype(np.float32).T * N**-0.5
    if causal:
        scores = scores + np.triu(np.full((ROWS, ROWS), -np.inf), 1)
    scores = scores - scores.max(-1, keepdims=True)
    weights = np.exp(scores) / np.exp(scores).sum(-1, keepdims=True)
    want = weights @ va.astype(np.float32)
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


def test_the_hatch_takes_a_head_axis(dev, heads):
    """Heads ride the ellipsis, so one call serves `(L,D)` and `(H,L,D)`."""
    qa, ka, va = heads
    stacked = [np.stack([a, a[::-1]]) for a in (qa, ka, va)]
    got = ktpugrad.attention(*(ktensor(a) for a in stacked)).numpy()
    one = ktpugrad.attention(ktensor(qa), ktensor(ka), ktensor(va)).numpy()
    assert got.shape == (2, ROWS, N)
    assert np.array_equal(got[0], one)


def test_the_hatch_result_composes_back_into_the_graph(dev, rowwise, operands):
    """It returns a KTPU Tensor, so the next matmul matches as it always did."""
    xa, _, _ = rowwise
    _, wa = operands
    got = ktpugrad.softmax(ktensor(xa)) @ ktensor(wa).T
    asts = sinks(got)
    assert len(asts) == 1
    assert ktpugrad.match_ast(asts[0]).kernel == "matmul"


def test_the_hatch_is_not_reached_by_falling_back(dev, rowwise):
    """A refused ast must still RAISE. The hatch is called, never fallen into.

    `softmax` itself now GENERATES, so the refused reduction here is one down a
    COLUMN, which folds across rows and no vector core walks.
    """
    xa, _, _ = rowwise
    with pytest.raises(ktpugrad.KTPUUnsupported, match="only the last axis|rank-3"):
        ktensor(xa).sum(axis=0).numpy()


def test_the_hatch_refuses_fp32(dev, rowwise):
    """Casting silently is how a frontend reports a precision it does not have."""
    xa, _, _ = rowwise
    with pytest.raises(ValueError, match="only fp16"):
        ktpugrad.softmax(Tensor(xa.astype(np.float32), device=ktpugrad.NAME))


def test_the_hatch_refuses_a_partial_block(dev):
    """`flash_attention` reads past a short block rather than padding it."""
    held = ktensor(np.zeros((ROWS + 6, N)))
    with pytest.raises(ValueError, match="whole number of"):
        ktpugrad.attention(held, held, held)


def test_the_hatch_refuses_a_gain_that_does_not_broadcast(dev, rowwise):
    """A wrong-shaped gain spread by guesswork is a wrong answer, not an error."""
    xa, _, _ = rowwise
    with pytest.raises(ValueError, match="does not broadcast"):
        ktpugrad.rmsnorm(ktensor(xa), ktensor(np.ones(N + 1)))


def test_the_hatch_names_install_when_the_seam_is_absent(dev, rowwise):
    """Without the seam, reading a KTPU tensor dies in tinygrad's RENDERER.

    That error names a program this backend never generates, which sends the
    reader to the wrong place; the hatch checks first and names `install()`.
    """
    xa, _, _ = rowwise
    held = ktensor(xa)
    ktpugrad.uninstall()
    try:
        with pytest.raises(RuntimeError, match="ktpugrad.install"):
            ktpugrad.softmax(held)
    finally:
        ktpugrad.install()


# --------------------------------------------------------------- the refusals
def test_softmax_is_four_kernels_and_only_the_matmul_matches(dev, operands):
    """The documented loss: four tinygrad kernels against one of ours."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = [ktpugrad.match_ast(a) for a in sinks((x @ w.T).softmax())]
    assert len(got) == 4
    assert [m.kernel for m in got if isinstance(m, ktpugrad.Match)] == ["matmul"]


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("fp32", lambda x, w: x.float() @ w.float().T),
        ("chain", lambda x, w: x * 2 + 1),
    ],
)
def test_a_shape_with_no_library_kernel_is_refused(dev, operands, name, build):
    """Refusing is the correct answer; assimilating one of these is a wrong one."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    assert all(ktpugrad.match_ast(a) is None for a in sinks(build(x, w)))


def test_a_batched_matmul_is_refused(dev, operands):
    """A leading axis makes it four axes, and the matcher reads three."""
    xa, wa = operands
    x = Tensor(np.stack([xa, xa]), device=ktpugrad.NAME)
    w = Tensor(wa, device=ktpugrad.NAME)
    assert all(ktpugrad.match_ast(a) is None for a in sinks(x @ w.T))


def test_a_refusal_says_what_it_saw(dev, operands):
    """A frontend that fails silently is worse than one that fails."""
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    with pytest.raises(ktpugrad.KTPUUnsupported, match="only the last axis|rank-3"):
        (x @ w.T).sum(axis=0).numpy()


# ----------------------------------------------------------------- the tiling
def test_the_defaults_are_kept_wherever_they_fit(dev):
    """A shape that ran before the chooser existed dispatches what it always did.

    The knobs are the ONLY thing this rung can vary, so a chooser that moved
    them for a shape already fitting would move every witness with them.
    """
    got, knobs = ktpugrad.plan(K.linear_silu, M, N, dev.machine.count("VC"))
    assert got is K.linear_silu
    assert knobs == dict(K.linear_silu.signature.knobs)


def test_a_grid_too_wide_for_the_defaults_is_retiled_not_refused(dev):
    """64x256 is the shape that used to raise, with advice this rung cannot take."""
    cores = dev.machine.count("VC")
    got, knobs = ktpugrad.plan(K.linear_silu, 64, 256, cores)
    assert got is K.linear_silu
    assert knobs["gm"] * knobs["gn"] > K.linear_silu.signature.knobs["gm"] ** 2
    xa = np.ones((64, 64), np.float16)
    wa = np.full((256, 64), 0.5, np.float16)
    assert got(dev.tensor(xa), dev.tensor(wa), **knobs).numpy().shape == (64, 256)


def test_past_the_fused_ceiling_the_COMPILER_stages_it(dev):
    """A fused grid covers `cores * 256` sub-tiles at most; beyond it, DRAM.

    `plan` hands back the kernel unchanged and `Kernel.relax` turns `fuse` off,
    which is what the twin used to be for.
    """
    got, knobs = ktpugrad.plan(K.linear_silu, 512, 512, dev.machine.count("VC"))
    assert got is K.linear_silu
    made = K.linear_silu.plan(
        dev.tensor(np.zeros((512, KD), np.float16)),
        dev.tensor(np.zeros((512, KD), np.float16)),
        **knobs,
    )
    assert made.knobs.get("fuse", True) is False


def test_a_retiled_shape_runs_end_to_end(dev):
    """The whole point: 64x256 through tinygrad, which used to be unreachable."""
    rng = np.random.default_rng(2)
    xa = (rng.standard_normal((64, KD)) * 0.5).astype(np.float16)
    wa = (rng.standard_normal((256, KD)) * 0.5).astype(np.float16)
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    got = (x @ w.T).silu().numpy()
    acc = xa.astype(np.float32) @ wa.astype(np.float32).T
    want = acc / (1 + np.exp(-acc))
    assert np.abs(got - want).max() / np.abs(want).max() < 0.05


# ------------------------------------------------------------------- the seam
def test_another_device_is_not_diverted(dev, operands):
    """`get_runner` is a global EVERY device lowers through."""
    xa, wa = operands
    x, w = xa.astype(np.float32), wa.astype(np.float32)
    got = (Tensor(x, device="CPU") @ Tensor(w, device="CPU").T).numpy()
    assert np.abs(got - x @ w.T).max() < 1e-3


def test_installing_twice_leaves_one_seam(dev, operands):
    """A second `install` must not stack, or `uninstall` restores a patch."""
    from tinygrad.engine import realize

    ktpugrad.install()
    assert realize.get_runner is ktpugrad.get_runner
    xa, wa = operands
    x, w = Tensor(xa, device=ktpugrad.NAME), Tensor(wa, device=ktpugrad.NAME)
    assert (x @ w.T).numpy().shape == (M, N)
