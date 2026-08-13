"""A whole model's placement, and whether it fits 16 GiB.

Compiler-only: a plan is names, byte counts and an order, so nothing here needs
a kernel or a device. The shapes are SDXL's, because the question the per-call
footprint cannot answer is whether a real model fits at all.
"""

import pytest
from kohakuaccel.plan import Plan, PlanError

MiB = 1 << 20
GiB = 1 << 30

#: The card. 16 GiB, and the reason any of this is careful.
ARENA = 16 * GiB

#: SDXL's UNet at 1024x1024: 2.6 GB of fp16 weights, and one transformer block
#: at C=1280, 20 heads of 64, context 77x2048.
UNET_WEIGHTS = 2600 * MiB
TOKENS, CHAN, CTX = 1024, 1280, 77


def block(plan: Plan, tag: str, src: str, tokens=TOKENS, chan=CHAN) -> str:
    """One BasicTransformerBlock: LN, self attn, LN, cross attn, LN, GEGLU.

    Chained -- `src` is the previous block's output. A buffer nothing writes is
    live for the whole run by definition, so a fixture that does not chain pins
    one activation per block and measures nothing.
    """
    act = tokens * chan * 2
    plan.weight(f"{tag}.w", 12 * chan * chan * 2, "qkv, out, and the two ff")
    for n in ("n1", "a1", "n2", "a2", "n3", "ff"):
        plan.act(f"{tag}.{n}", act)
        plan.step(f"{tag}.{n}", reads=(src, f"{tag}.w"), writes=(f"{tag}.{n}",))
        src = f"{tag}.{n}"
    return src


def test_one_block_fits_with_room_to_spare():
    """The unit the compiler actually schedules."""
    p = Plan(ARENA, align=256)
    p.input("x", TOKENS * CHAN * 2)
    p.step("upload", writes=("x",))
    out = block(p, "b0", "x")
    p.output("y", TOKENS * CHAN * 2)
    p.step("readout", reads=(out,), writes=("y",))
    place = p.solve()
    assert not place.verify(), place.verify()
    assert place.total_bytes < ARENA


def test_a_deep_stack_reuses_activations_instead_of_growing():
    """Seventy blocks must not cost seventy blocks of activations.

    Each block's intermediates die inside it, so the peak is one block's worth
    however many there are. Without reuse this is what runs the card out.
    """
    deep, flat = Plan(ARENA), Plan(ARENA)
    for p in (deep, flat):
        p.input("x", TOKENS * CHAN * 2)
        p.step("upload", writes=("x",))
        src = "x"
        for i in range(70):
            src = block(p, f"b{i}", src)
        p.output("y", TOKENS * CHAN * 2)
        p.step("readout", reads=(src,), writes=("y",))
    packed = deep.solve(reuse=True)
    apart = flat.solve(reuse=False)
    assert (
        packed.peak_act_bytes < apart.peak_act_bytes / 10
    ), f"{packed.peak_act_bytes:,} against {apart.peak_act_bytes:,}"
    assert not packed.verify(), packed.verify()


def test_the_weights_dominate_and_are_pinned_first():
    """A weight is uploaded once; its address must not move when a layer is
    added, or every later weight is re-sent over a 100 kB/s link."""
    one, two = Plan(ARENA), Plan(ARENA)
    for p in (one, two):
        p.input("x", TOKENS * CHAN * 2)
        p.step("upload", writes=("x",))
    block(one, "b0", "x")
    block(two, "b1", block(two, "b0", "x"))
    assert one.solve().at["b0.w"] == two.solve().at["b0.w"]


def test_a_model_too_big_says_which_half_is_too_big():
    """Weights against peak activations, because they have different fixes."""
    p = Plan(1 * GiB)
    p.weight("w", 2 * GiB)
    p.act("a", MiB)
    p.step("s", reads=("w",), writes=("a",))
    with pytest.raises(PlanError, match="weights"):
        p.solve()


def test_a_full_unet_of_weights_leaves_room_for_the_activations():
    """The question 16 GiB is actually about.

    Weights resident and one block live: if this does not fit, SDXL does not
    run whatever the schedule does.
    """
    p = Plan(ARENA)
    p.weight("unet", UNET_WEIGHTS, "every layer, resident")
    p.input("latent", TOKENS * CHAN * 2)
    p.step("upload", writes=("latent",))
    out = block(p, "mid", "latent")
    p.output("eps", TOKENS * CHAN * 2)
    p.step("readout", reads=(out,), writes=("eps",))
    place = p.solve()
    assert not place.verify(), place.verify()
    assert place.total_bytes < ARENA, place.report()
    assert place.weight_bytes > place.peak_act_bytes


def test_verify_catches_a_plan_whose_steps_lie_about_the_order():
    """A buffer read long after the allocator gave its span away.

    The measured failure this exists for: `verify()` passing while the runner
    reads bytes somebody else owns.
    """
    p = Plan(ARENA)
    p.act("early", 64 * MiB)
    p.act("late", 64 * MiB)
    p.act("out", 64 * MiB)
    p.step("make", writes=("early",))
    p.step("other", writes=("late",))
    p.step("use", reads=("early", "late"), writes=("out",))
    place = p.solve()
    assert not place.verify(), "early is live until step 2 and must not be reused"
