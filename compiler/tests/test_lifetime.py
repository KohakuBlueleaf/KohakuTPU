"""Temps that are never live together share bytes, and the proof that they can.

Reuse is the one optimisation in a memory manager that can return WRONG NUMBERS
rather than merely run out: two buffers on one span produce plausible answers and
no fault. So this file is mostly the invariant, not the saving --
:func:`~kohakuaccel.lifetime.conflicts` over a few thousand randomised
schedules, and every fixture run twice and compared element for element.

`tests/test_arena.py` does the same job for the heap underneath. This is the
layer that decides WHEN a span dies.

The kernels are FIXTURES (rule 2b). `staged_norm` is the vehicle for anything
about sharing: seven temps over eight passes, some provably never co-live.
"""

import gc
import importlib
import random

import numpy as np
import pytest
from kohakuaccel.lifetime import (
    AliasError,
    Span,
    Writes,
    aliases,
    conflicts,
    lifetimes,
    pack,
)
from kohakutpu.model import SimDevice

from tests.fixtures import (
    chained,
    masked,
    mm,
    mm_silu,
    residual,
    rownorm,
    scale,
    staged_norm,
)

#: The submodule, not the decorator: `kohakuaccel.lang` re-exports `kernel`, so
#: the dotted name resolves to a function and monkeypatch cannot reach past it.
KERNEL = importlib.import_module("kohakuaccel.lang.kernel")

#: Every fixture, at a shape small enough to run under the models.
CASES = [
    ("scale", scale, [(64, 64)], {}),
    ("residual", residual, [(64, 64), (64, 64)], {}),
    ("rownorm", rownorm, [(64, 64)], {}),
    ("chained", chained, [(64, 64)], {}),
    ("staged_norm", staged_norm, [(64, 64)] * 3, {}),
    ("mm", mm, [(64, 128), (64, 128)], {}),
    ("mm_silu", mm_silu, [(64, 128), (64, 128)], {}),
    ("masked", masked, [(2, 64, 64)], {"block": 64}),
]

NORM = [(64, 64)] * 3


def run(kern, shapes, knobs, reuse, seed=11):
    """`kern` on a fresh device. Returns ``(result, bytes the arena grew by)``."""
    rng = np.random.default_rng(seed)
    dev = SimDevice(size=1 << 26)
    dev.reuse_temps = reuse
    args = [dev.tensor(np.asarray(rng.normal(0, 1, s), np.float16)) for s in shapes]
    base = dev.arena.peak
    out = kern(*args, **knobs)
    return np.asarray(out.numpy()), dev.arena.peak - base


def layernorm_ref(x, w, b, eps=1e-5):
    """What `staged_norm` computes, in float64."""
    xf = np.float64(x)
    mu = xf.mean(-1, keepdims=True)
    var = ((xf - mu) ** 2).mean(-1, keepdims=True)
    return (xf - mu) / np.sqrt(var + eps) * np.float64(w) + np.float64(b)


# ------------------------------------------------------------------ lifetimes
def test_a_lifetime_is_the_first_write_to_the_last_touch():
    steps = [({}, {"a"}), ({"a"}, {"b"}), ({"b"}, {"c"}), ({"c"}, {"d"})]
    life = lifetimes(steps, ["a", "b", "c"])
    assert life["a"] == (0, 1)
    assert life["b"] == (1, 2)
    assert life["c"] == (2, 3)


@pytest.mark.parametrize(
    ("why", "steps"),
    [
        ("no step mentions it", [({"x"}, {"y"})]),
        ("written and never read", [({}, {"a"}), ({"x"}, {"y"})]),
        ("read and never written", [({"a"}, {"y"}), ({"x"}, {"y"})]),
        ("read before it is written", [({"a"}, {"y"}), ({}, {"a"})]),
        ("read at the step that first writes it", [({"a"}, {"a"}), ({"a"}, {"y"})]),
    ],
)
def test_a_lifetime_the_schedule_cannot_establish_is_the_whole_run(why, steps):
    """Default-refuse. A backend that does not record what a statement touches
    must lose the SAVING, never the answer."""
    assert lifetimes(steps, ["a"])["a"] == (-1, len(steps)), why


def test_keep_pins_a_buffer_for_the_whole_run():
    """What the caller owns: operands, results, and anything cached past the call."""
    steps = [({}, {"a"}), ({"a"}, {"b"})]
    assert lifetimes(steps, ["a"], keep=["a"])["a"] == (-1, 2)


# --------------------------------------------------------------------- packing
def test_disjoint_lifetimes_share_one_span():
    """THE SAVING, stated where it can be made to happen on purpose.

    A kernel is the wrong place to assert this: the compiler fuses elementwise
    passes until the surviving temps overlap, so whether any two are disjoint is
    a property of the fusion, not of the packer.
    """
    life = {"a": (0, 1), "b": (2, 3)}
    offsets, high = pack(["a", "b"], {"a": 1024, "b": 1024}, life)
    assert offsets["a"] == offsets["b"]
    assert high == 1024


def test_overlapping_lifetimes_never_share():
    life = {"a": (0, 3), "b": (1, 2)}
    offsets, high = pack(["a", "b"], {"a": 1024, "b": 1024}, life)
    assert offsets["a"] != offsets["b"]
    assert high == 2048


def test_a_steps_output_does_not_land_on_an_operand_it_is_still_reading():
    """THE ORDERING RULE, and it is not the obvious one.

    `b` dies at step 1 and `c` is born at step 1. They are live at the same
    instant, so freeing before allocating would put the result on top of the
    operand -- in place, silently, at whatever the unit had reached.
    """
    life = {"b": (0, 1), "c": (1, 2)}
    offsets, high = pack(["b", "c"], {"b": 1024, "c": 1024}, life)
    assert offsets["b"] != offsets["c"]
    assert high == 2048


def test_a_packing_is_aligned_and_inside_its_block():
    sizes = {"a": 300, "b": 5, "c": 4096}
    life = {"a": (0, 1), "b": (0, 4), "c": (2, 3)}
    offsets, high = pack(list(sizes), sizes, life, align=256)
    assert all(v % 256 == 0 for v in offsets.values())
    assert all(offsets[n] + sizes[n] <= high for n in sizes)


def test_a_random_schedule_never_places_two_co_live_buffers_together():
    """The invariant, over a few thousand generated schedules.

    Case-based tests cover the packer's branches; this covers the ones nobody
    thought of, which is where an aliasing bug actually comes from.
    """
    rng = random.Random(0)
    for _ in range(2000):
        n = rng.randint(1, 12)
        steps = rng.randint(1, 10)
        names = [f"t{i}" for i in range(n)]
        sizes = {x: rng.randint(1, 4096) for x in names}
        life = {}
        for x in names:
            first = rng.randint(0, steps)
            life[x] = (first, rng.randint(first, steps))
        offsets, high = pack(names, sizes, life, align=256)
        spans = [Span(x, offsets[x], sizes[x], life[x]) for x in names]
        assert not conflicts(spans), f"{life} -> {offsets}"
        assert all(offsets[x] + sizes[x] <= high for x in names)


def test_conflicts_finds_an_overlap_that_is_really_there():
    """The check has to be able to FAIL, or a green run proves nothing."""
    bad = [Span("a", 0, 2048, (0, 3)), Span("b", 1024, 2048, (1, 2))]
    assert conflicts(bad)
    assert not conflicts([Span("a", 0, 2048, (0, 1)), Span("b", 1024, 2048, (2, 3))])


# -------------------------------------------------------------------- aliasing
def test_aliases_are_empty_when_nothing_is_shared():
    """What makes the read guard free on a placement that reused nothing."""
    spans = [Span("a", 0, 1024, (0, 1)), Span("b", 1024, 1024, (0, 1))]
    assert not Writes(aliases(spans)).sharing


def test_a_read_of_an_overwritten_buffer_is_caught_at_the_read():
    """The silent-wrong-answer class, and the reason reuse needs a guard.

    `a` and `b` share bytes. Writing `b` destroys `a`, and the read of `a` that
    follows returns `b`'s numbers with nothing to say so.
    """
    spans = [Span("a", 0, 2048, (0, 3)), Span("b", 0, 2048, (1, 2))]
    guard = Writes(aliases(spans))
    assert guard.sharing
    guard.wrote("a")
    guard.check("stage 1", "a")
    guard.wrote("b")
    with pytest.raises(AliasError, match="overwritten by"):
        guard.check("stage 2", "a")


def test_a_buffer_nothing_aliases_is_never_wrong():
    guard = Writes(aliases([Span("a", 0, 1024, (0, 1)), Span("b", 2048, 1024, (0, 1))]))
    guard.check("stage 0", "a", "b")


# --------------------------------------------------------------- through a call
@pytest.mark.parametrize(
    ("name", "kern", "shapes", "knobs"), CASES, ids=[c[0] for c in CASES]
)
def test_reuse_returns_exactly_the_same_numbers(name, kern, shapes, knobs):
    """THE claim. Sharing a span must change the address and nothing else.

    Element for element, not to a tolerance: reuse does no arithmetic, so any
    difference at all is a buffer landing where it should not have.
    """
    plain, _ = run(kern, shapes, knobs, False)
    shared, _ = run(kern, shapes, knobs, True)
    assert np.array_equal(np.nan_to_num(plain, nan=-7), np.nan_to_num(shared, nan=-7))


@pytest.mark.parametrize(
    ("name", "kern", "shapes", "knobs"), CASES, ids=[c[0] for c in CASES]
)
def test_the_footprint_predicts_the_reused_peak_too(name, kern, shapes, knobs):
    """Predicting the default is easy; predicting what reuse does is the point."""
    dev = SimDevice(size=1 << 26)
    dev.reuse_temps = True
    rng = np.random.default_rng(11)
    args = [dev.tensor(np.asarray(rng.normal(0, 1, s), np.float16)) for s in shapes]
    want = kern.footprint(*args, **knobs)
    assert want.reused
    before = dev.arena.peak
    kern(*args, **knobs)
    assert dev.arena.peak - before == want.peak


def test_reuse_is_off_unless_it_is_asked_for():
    """Additive: a runtime nobody configured allocates what it always allocated."""
    dev = SimDevice(size=1 << 26)
    assert dev.reuse_temps is False
    made = [dev.tensor(np.zeros((64, 64), np.float16))] * 3
    assert not staged_norm.footprint(*made).reused


def test_reuse_lowers_the_peak_where_lifetimes_allow():
    """`staged_norm` has seven temps and not all are ever live together.

    The saving is 8,192 -- one temp -- rather than the four it looks entitled
    to: a band that writes and reads a temp inside ONE stage default-refuses
    its lifetime, and three of the seven end up whole-run that way.
    """
    _, plain = run(staged_norm, NORM, {}, False)
    _, shared = run(staged_norm, NORM, {}, True)
    assert shared < plain
    assert shared == 114688 and plain == 122880


def test_a_raising_stage_leaves_ONE_scratch_block_and_empty_cache_returns_it():
    """The shared temps are one span, so a failure cannot strand a pile of them.

    The block SURVIVES the call by design -- `Runtime.scratch` keeps it for the
    next one rather than re-paying alloc/free per call -- so the invariant is
    "exactly one span, and `empty_cache` gets it back", not "it was freed".

    The raise is caught HERE rather than by `pytest.raises`: its `ExceptionInfo`
    keeps the traceback, the traceback keeps the frame, and the frame keeps the
    RESULT tensor, which would read as a second leak.
    """
    dev = SimDevice(size=1 << 26)
    dev.reuse_temps = True
    args = [dev.tensor(np.zeros((64, 64), np.float16)) for _ in range(3)]
    span = staged_norm.plan(*args).placement(dev.arena.align)[1]
    assert span, "staged_norm shares no temps, so this proves nothing"

    def fail(*a, **k):
        raise RuntimeError("dispatch went wrong")

    dev.dispatch = fail
    raised = ""
    try:
        staged_norm(*args)
    except RuntimeError as exc:
        raised = str(exc)
    del dev.dispatch
    gc.collect()
    assert raised == "dispatch went wrong"

    kept = sum(sum(b.nbytes for b in a.buffers.values()) for a in args)
    consts = sum(b.nbytes for b in dev._constants.values())
    assert dev.arena.used == kept + consts + span, "more than one span survived"
    dev.empty_cache()
    assert dev.arena.used == kept, "empty_cache did not return the scratch"


# ----------------------------------------------------------------- the guard
def collided(kern, args, align):
    """Put every temp of `kern` at one address, and return the compilation.

    The bug the read guard exists for, injected: a placement that says two
    co-live buffers may share. The caller must clear `_placed` afterwards.
    """
    compiled = kern.plan(*args)
    temps = [n for n in compiled.temps if n not in compiled.tables]
    span = compiled.placement(align)[1]
    compiled._placed[align] = ({n: 0 for n in temps}, span)
    return compiled


def test_a_broken_placement_returns_an_answer_when_nothing_checks(monkeypatch):
    """WHY THE GUARD IS NOT DECORATIVE. Measured, not argued.

    Every temp of `staged_norm` on one address gives 138% error with no NaN, no
    saturation and no fault. Nothing downstream of this can tell; it is a
    plausible tensor of plausible numbers.
    """
    monkeypatch.setattr(KERNEL, "_guard", lambda *a: Writes({}))
    dev = SimDevice(size=1 << 26)
    dev.reuse_temps = True
    rng = np.random.default_rng(4)
    raw = [np.asarray(rng.normal(0, 1, (64, 64)), np.float16) for _ in range(3)]
    args = [dev.tensor(a) for a in raw]
    compiled = collided(staged_norm, args, dev.arena.align)
    try:
        got = np.asarray(staged_norm(*args).numpy(), np.float64)
    finally:
        compiled._placed.clear()

    ref = layernorm_ref(*raw)
    assert not np.isnan(got).any() and not np.isinf(got).any()
    assert np.abs(got - ref).max() / np.abs(ref).max() > 1.0


def test_the_guard_raises_at_the_read_instead():
    """The same broken placement, checked. It must fail rather than answer."""
    dev = SimDevice(size=1 << 26)
    dev.reuse_temps = True
    args = [dev.tensor(np.zeros((64, 64), np.float16)) for _ in range(3)]
    compiled = collided(staged_norm, args, dev.arena.align)
    try:
        with pytest.raises(AliasError, match="read here but was overwritten"):
            staged_norm(*args)
    finally:
        compiled._placed.clear()


def test_one_tensor_passed_as_two_operands_is_not_a_fault():
    """`a + a` puts one address on two ports. Only a WRITE can make a read wrong,
    so co-live buffers nothing writes are left alone."""
    dev = SimDevice(size=1 << 26)
    x = dev.tensor(np.asarray(np.full((64, 64), 3), np.float16))
    assert np.allclose(np.asarray(residual(x, x).numpy()), 6)


def test_a_folded_extent_does_not_go_stale_across_shapes():
    """A CACHED CONSTANT MUST BE THE ONE THAT WAS ASKED FOR.

    `TpuBackend._const` names a folded constant for its SYMBOL, so an extent
    gives `const<N>` for every N while `_scalar` resolves the array per
    compilation. Keyed on the name alone, the second shape read the first
    shape's array: measured 0.334 against 8.9e-4, no fault, no warning.
    """
    rng = np.random.default_rng(9)
    dev = SimDevice(size=1 << 26)
    for cols in (64, 128):
        made = [np.asarray(rng.normal(0, 1, (64, cols)), np.float16) for _ in range(3)]
        got = staged_norm(*[dev.tensor(a) for a in made]).numpy()
        ref = layernorm_ref(*made)
        assert np.abs(np.float64(got) - ref).max() / np.abs(ref).max() < 5e-3, cols


def test_the_guard_sees_sharing_only_where_reuse_put_it(monkeypatch):
    """Off, nothing aliases and every check returns at the first lookup; on, the
    shared temps appear, which is when the guard is worth running."""
    real, seen = KERNEL._guard, []
    monkeypatch.setattr(KERNEL, "_guard", lambda *a: seen.append(real(*a)) or seen[-1])
    for reuse in (False, True):
        dev = SimDevice(size=1 << 26)
        dev.reuse_temps = reuse
        rng = np.random.default_rng(2)
        args = [
            dev.tensor(np.asarray(rng.normal(0, 1, (64, 64)), np.float16))
            for _ in range(3)
        ]
        staged_norm(*args)
        assert seen[-1].sharing is reuse
