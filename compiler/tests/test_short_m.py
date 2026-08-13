"""The `linear_*` family at a SHORT M, graded by MAX error and not by mean.

An adaLN-Zero conditioning projection's M is the batch, so a DiT reaches this
regime with `nn.Linear` and nothing else in a block does. The defect it found:
`Buffer.parts` sized the elementwise grid on the LOGICAL element count while the
pass walked the padded `Tile` image, so the rows of the last tile were left
unwritten past some column -- 100% error on those elements while p50 stayed at
1e-01 and every shipped test, all at M = 32, 64 or 128, stayed green.

GRADED BY MAX, relative to the reference's own peak. A mean hides this entirely:
the adaLN projection scored max 1.00e+00 beside p50 1.43e-01.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, iface
from kohakutpu.lang import kernel
from kohakutpu.lang.backend import _per, _room, _span, _stride
from kohakutpu.lang.buffers import PART
from kohakutpu.model import SimDevice

from kohakutpu import kernels as K
from kohakutpu import lang as L

MD, NDIM = dims("M, N")

#: `ND` is where the boundary is richest: at 1024 the padded 32-row result is
#: four parts, so M 1-24 were wrong and 25-32 right. `KD` only costs time.
KD, ND = 64, 1024

#: fp16 operands through an MXFP7 contraction. Every correct point measures
#: 1.4e-02 to 2.2e-02; a wrong one measured ~1.0.
NOISE = 5e-2

ARENA = 1 << 27


def _operands(m, k, n, extra, seed):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    return (
        x,
        w,
        [(rng.standard_normal((m, n)) * 0.5).astype(np.float16) for _ in range(extra)],
    )


def _err(got, want) -> float:
    return float(np.abs(got - want).max() / np.abs(want).max())


def linear_add_err(m, n=ND, seed=0, regrid=False, **knobs) -> float:
    """`(x @ w.T) + b` at `m x KD x n`, as max error over the reference's peak.

    `regrid` sizes the elementwise grid from the layout, which is the OTHER
    program this shape compiles to and has to be just as right.
    """
    x, w, (b,) = _operands(m, KD, n, 1, seed)
    dev = SimDevice(size=ARENA)
    dev.regrid = regrid
    got = K.linear_add(dev.tensor(x), dev.tensor(w), dev.tensor(b), **knobs).numpy()
    want = x.astype(np.float64) @ w.astype(np.float64).T + b.astype(np.float64)
    return _err(got, want)


def linear_silu_err(m, n=ND, seed=0, fused=False, regrid=False, **knobs) -> float:
    """`silu(x @ w.T)` at `m x KD x n`, as max error over the reference's peak.

    `fused` picks the epilogue that rides the accumulator over the NoC; the
    default stages through a DRAM temp, which is what `relax` falls back to once
    the grid outgrows the vector cores -- at a DiT's N = 6144 that is every M.
    """
    x, w, _ = _operands(m, KD, n, 0, seed)
    dev = SimDevice(size=ARENA)
    dev.regrid = regrid
    if not fused:
        knobs = {**knobs, "fuse": False}
    got = K.linear_silu(dev.tensor(x), dev.tensor(w), **knobs).numpy()
    held = x.astype(np.float64) @ w.astype(np.float64).T
    return _err(got, held / (1.0 + np.exp(-held)))


@pytest.mark.parametrize("m", range(1, 65))
@pytest.mark.parametrize("seed", [0, 1])
def test_linear_add_is_right_at_every_short_m(m, seed):
    """M = 1..64 densely. The tile is 32 rows, so this covers two of them.

    Wrong for M 1-24 and 33-56 before the fix, right for 25-32 and 57-64 -- the
    boundary being where the padding stops fitting in the last part.
    """
    assert linear_add_err(m, seed=seed) < NOISE


@pytest.mark.parametrize("m", range(1, 65))
@pytest.mark.parametrize("seed", [0, 1])
def test_linear_silu_is_right_at_every_short_m(m, seed):
    assert linear_silu_err(m, seed=seed) < NOISE


@pytest.mark.parametrize("m", [1, 2, 3, 4, 5, 8, 12, 13, 16])
def test_the_fused_epilogue_is_right_at_a_short_m_too(m):
    """The other half of the family: no temp, no elementwise pass, no defect.

    Pinned rather than assumed. `gn=64` because `ktpugrad.plan` reaches for
    `gn=128` here, whose `gn * nk` fill is 256 entries and REFUSED at 255; past
    M = 16 this tiling wants 16 receivers and the grid refuses in turn.
    """
    assert linear_silu_err(m, fused=True, gm=4, gn=64) < NOISE


@pytest.mark.parametrize("n", [64, 128, 256, 320, 512, 1024, 3072, 6144])
def test_a_wide_n_is_right_at_a_batch_sized_m(n):
    """M = 2 and N up to a DiT's 6D.

    32 padded rows by 256 columns is 8,192 elements, which is `part`: before the
    fix this was right at N = 64, 128 and 256 -- one part -- and wrong from 320.
    """
    assert linear_add_err(2, n=n) < NOISE


@pytest.mark.parametrize("gm", [1, 2, 4, 8, 16])
@pytest.mark.parametrize("m", [8, 16])
def test_the_answer_does_not_turn_on_the_accumulator_tile(m, gm):
    """The tile is `LANES * gm` rows, and the defect's boundary moved with it.

    At M = 8 only gm 1 and 2 were right; at M = 16, gm 4 joined them. Padding is
    not arithmetic, so the answer must not depend on `gm` at all.
    """
    assert linear_add_err(m, gm=gm) < NOISE


def _identity(v):
    return v


def _relu(v):
    return np.maximum(v, 0.0)


def _silu(v):
    return v / (1.0 + np.exp(-v))


def _gelu(v):
    return v * 0.5 * (1 + np.tanh(0.7978845608028654 * (v + 0.044715 * v**3)))


#: Each epilogue, its identity operands, its activation, and its knobs. At this
#: shape the grid outgrows the cores, so `fuse=False` is what `relax` picks.
EPILOGUES = [
    (K.linear_add, (0.0,), _identity, {}),
    (K.linear_add_relu, (0.0,), _relu, {}),
    (K.linear_add_silu, (0.0,), _silu, {}),
    (K.linear_add_gelu, (0.0,), _gelu, {}),
    (K.linear_gate_add, (1.0, 0.0), _identity, {}),
    (K.linear_scale, (), _identity, {"fuse": False}),
    (K.linear_relu, (), _relu, {"fuse": False}),
    (K.linear_silu, (), _silu, {"fuse": False}),
    (K.linear_gelu, (), _gelu, {"fuse": False}),
]


@pytest.mark.parametrize("regrid", [False, True], ids=["traced", "regrid"])
@pytest.mark.parametrize(
    "fn, fills, epilogue, knobs",
    EPILOGUES,
    ids=[f"{fn.name}{'_staged' if k else ''}" for fn, _, _, k in EPILOGUES],
)
def test_every_epilogue_is_right_at_the_adaln_shape(fn, fills, epilogue, knobs, regrid):
    """M = 2 into N = 6144 -- the adaLN-Zero projection, M being the batch.

    Every one of these scored between 0.98 and 1.16 at this shape before the
    fix. Both ways round: this shape compiles to two programs -- 2 instances of
    98,304 and 24 of 8,192 -- and both have to be right.
    """
    m, n = 2, 6144
    x, w, _ = _operands(m, KD, n, 0, 0)
    dev = SimDevice(size=ARENA)
    dev.regrid = regrid
    args = [dev.tensor(x), dev.tensor(w)]
    args += [dev.tensor(np.full((m, n), v, np.float16)) for v in fills]
    got = fn(*args, **knobs).numpy()
    want = epilogue(x.astype(np.float64) @ w.astype(np.float64).T)
    assert _err(got, want) < NOISE


# --------------------------- `Runtime.regrid`: the grid sized from the LAYOUT
# rather than from the logical count that was never what the pass walked
class Shaped:
    """What `solve` needs of an argument, without a device to put it on."""

    def __init__(self, shape) -> None:
        self.shape = shape


def _cover(fn, bound, regrid, **knobs):
    """``(every instance's span, the room, the stride)`` of the elementwise stage."""
    dev = SimDevice(size=ARENA)
    got = fn.compile(dev.machine, iface.solve(fn.signature, bound), regrid, **knobs)
    stage = next(s for s in got.stages if s.unit == "VC" and s.instances)
    stride = _stride(got, stage, stage.instances[0].stmts[0], _per(got))
    spans = [_span(got, i.stmts[0], stride) for i in stage.instances]
    return spans, _room(got, stage.instances[0].stmts[0]), stride


def _linear_add(m, n) -> dict:
    return {"x": Shaped((m, KD)), "w": Shaped((n, KD)), "b": Shaped((m, n))}


def test_the_wider_grid_is_off_unless_it_is_asked_for():
    """Additive: a runtime nobody configured emits the program it always did.

    It is a TRADE and not a fix -- `linear_gelu_separated` at this shape costs
    2.6x the instruction stream for 2x the cores -- so it is the caller's to
    make, exactly as `reuse_temps` is. Both covers are correct; only one is
    the default.
    """
    assert SimDevice(size=ARENA).regrid is False
    spans, room, _ = _cover(K.linear_add, _linear_add(2, 6144), False)
    assert spans == [98304, 98304]
    assert sum(spans) == room == 196608


def test_the_adaln_grid_is_as_wide_as_the_work():
    """M=2, N=6144 asked for: 24 instances of 8,192, where it ran 2 of 98,304.

    Both cover the same 196,608 elements -- covering them is the fix this
    followed and is not what changed. What the grid decides is how many vector
    cores share the work, and two of four were idle.
    """
    spans, room, stride = _cover(K.linear_add, _linear_add(2, 6144), True)
    assert room == 196608
    assert stride == PART
    assert spans == [PART] * 24
    assert sum(spans) == room


@pytest.mark.parametrize(
    ("m", "n", "want"),
    [
        (2, 320, [8192, 2048]),
        (2, 1024, [8192] * 4),
        (24, 1024, [8192] * 4),
        (33, 1024, [8192] * 8),
        (32, 1024, [8192] * 4),
        (64, 1024, [8192] * 8),
    ],
)
def test_every_instance_takes_a_part_and_the_last_takes_the_tail(m, n, want):
    """The instances partition the padded image: no gap, no overlap, no tail.

    M = 32 and 64 are the shapes that were always right, and they are here to
    show the grid does not move for them even when asked -- four and eight,
    which is what they already were.
    """
    spans, room, _ = _cover(K.linear_add, _linear_add(m, n), True)
    assert spans == want
    assert sum(spans) == room


@pytest.mark.parametrize("m", [1, 2, 8, 20, 24, 33, 48, 56])
def test_the_wider_grid_is_right_at_every_m_that_moves(m):
    """The other program, at the M values whose grid lengthens. Same answer.

    The dense sweep above grades the default; these are the shapes where the
    two differ at all, so they are where a wider grid could be wrong alone.
    """
    assert linear_add_err(m, regrid=True) < NOISE


@pytest.mark.parametrize("n", [320, 512, 1024, 3072, 6144])
def test_the_wider_grid_is_right_at_every_n_that_moves(n):
    """N = 320 is the one with a PARTIAL tail: 8,192 then 2,048."""
    assert linear_add_err(2, n=n, regrid=True) < NOISE


def _elementwise(compiled) -> int:
    """Instances in the one whole-buffer elementwise stage."""
    stage = next(s for s in compiled.stages if s.unit == "VC" and s.instances)
    return len(stage.instances)


def test_the_flag_reaches_the_call_and_the_cache_keeps_them_apart():
    """Set on the DEVICE and read at the call, as `reuse_temps` is.

    Both plans are the same kernel, machine and shape and differ only in the
    flag, so a cache keyed without it would hand the second the first's
    program -- the trap `Compiled.tag` documents, where 64x128 after 64x64
    silently read the earlier shape's array.
    """
    x, w, (b,) = _operands(2, KD, 6144, 1, 0)
    dev = SimDevice(size=ARENA)
    held = [dev.tensor(x), dev.tensor(w), dev.tensor(b)]

    assert _elementwise(K.linear_add.plan(*held)) == 2
    dev.regrid = True
    assert _elementwise(K.linear_add.plan(*held)) == 24
    dev.regrid = False
    assert _elementwise(K.linear_add.plan(*held)) == 2


@pytest.mark.parametrize(("first", "second"), [(True, False), (False, True)])
def test_a_folded_constant_is_not_shared_between_the_two_grids(first, second):
    """A folded array is one instance's STRIDE long, and the two strides differ.

    Staged, `linear_scale` folds `s` into an array as long as one pass. With
    the layout-sized grid run first, the traced grid read ITS 8,192-long array
    over a 98,304-element pass and scored 1.00, silently -- the constant cache
    is keyed on `Compiled.tag`, which did not name the grid until it did.
    """
    x, w, _ = _operands(2, KD, 6144, 0, 0)
    want = (x.astype(np.float64) @ w.astype(np.float64).T) * 2.0
    dev = SimDevice(size=ARENA)
    held = [dev.tensor(x), dev.tensor(w)]

    dev.regrid = first
    one = K.linear_scale(*held, s=2.0, fuse=False).numpy()
    dev.regrid = second
    two = K.linear_scale(*held, s=2.0, fuse=False).numpy()
    assert _err(one, want) < NOISE
    assert _err(two, want) < NOISE


def test_a_reduction_keeps_its_grid_of_one():
    """A `RowReduceKernel` covers whole rows in ONE pass, whatever the room is.

    Two row sums are what the padded-room arithmetic would most like to split,
    and 128x128 is twice a `part`. Split, each instance would run the WHOLE
    reduction at its own base and the later ones would win. Staged fixture: the
    fused form folds inside a chain, where the row IS the step.
    """

    @kernel
    def group_norm_staged(
        x=L.In(..., MD, NDIM),
        w=L.In(..., MD, NDIM),
        b=L.In(..., MD, NDIM),
        y=L.Out(..., MD, NDIM),
    ):
        mu, dev_, sq, var, norm = (L.temp(MD, NDIM) for _ in range(5))
        # Each fold on its OWN statement, so both are `reduce` and neither is a
        # chain fold: a chain fold steps one row and is not what this pins.
        mu <<= L.row_sum(x)
        dev_ <<= x - mu / NDIM
        sq <<= (dev_ / NDIM) * dev_
        var <<= L.row_sum(sq)
        norm <<= dev_ * L.rsqrt(var + 1e-5)
        y <<= norm * w + b

    dev = SimDevice(size=ARENA)
    shape = Shaped((128, 128))
    got = group_norm_staged.compile(
        dev.machine,
        iface.solve(group_norm_staged.signature, {"x": shape, "w": shape, "b": shape}),
        True,
    )
    folds = [
        s
        for s in got.stages
        if s.instances and any(st.kind == "reduce" for st in s.instances[0].stmts)
    ]
    assert len(folds) == 2
    assert [len(s.instances) for s in folds] == [1, 1]
