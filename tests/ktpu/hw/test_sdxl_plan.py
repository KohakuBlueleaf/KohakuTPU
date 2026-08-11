"""Planning a UNet: topology read off the shapes, and the skip stack's lifetimes.

Built from a synthetic shape dictionary rather than a checkpoint, because
`modules` takes shapes and nothing else -- which is exactly what makes it
testable without a 6.5 GiB file, and what lets it plan a checkpoint whose widths
differ from the one it was written against.
"""

import pytest

from ktpu.hw import sdxl
from ktpu.hw.memplan import PlanError

GB = 2**30


def mini(ch=(64, 128), depth=2, ctx=256, tdim=256):
    """A two-stage UNet in SDXL's parameter naming. Small, same structure."""
    s, c0, c1 = {}, *ch
    s["time_embed.0.weight"] = (tdim, c0)
    s["time_embed.2.weight"] = (tdim, tdim)
    s["input_blocks.0.0.weight"] = (c0, 4, 3, 3)
    s["input_blocks.0.0.bias"] = (c0,)
    for i in (1, 2):
        _res(s, f"input_blocks.{i}.0", c0, c0, tdim)
    s["input_blocks.3.0.weight"] = (c0, c0, 3, 3)
    _res(s, "input_blocks.4.0", c0, c1, tdim, skip=True)
    _tf(s, "input_blocks.4.1", c1, depth, ctx)
    _res(s, "middle_block.0", c1, c1, tdim)
    _res(s, "output_blocks.0.0", c1 + c1, c1, tdim, skip=True)
    s["output_blocks.0.2.weight"] = (c1, c1, 3, 3)
    _res(s, "output_blocks.1.0", c1 + c0, c0, tdim, skip=True)
    _res(s, "output_blocks.2.0", c0 + c0, c0, tdim, skip=True)
    _res(s, "output_blocks.3.0", c0 + c0, c0, tdim, skip=True)
    s["out.0.weight"] = (c0,)
    s["out.2.weight"] = (4, c0, 3, 3)
    return s


def _res(s, p, cin, cout, tdim, skip=False):
    s[f"{p}.in_layers.0.weight"] = (cin,)
    s[f"{p}.in_layers.2.weight"] = (cout, cin, 3, 3)
    s[f"{p}.in_layers.2.bias"] = (cout,)
    s[f"{p}.emb_layers.1.weight"] = (cout, tdim)
    s[f"{p}.out_layers.0.weight"] = (cout,)
    s[f"{p}.out_layers.3.weight"] = (cout, cout, 3, 3)
    s[f"{p}.out_layers.3.bias"] = (cout,)
    if skip:
        s[f"{p}.skip_connection.weight"] = (cout, cin, 1, 1)


def _tf(s, p, c, depth, ctx):
    s[f"{p}.norm.weight"] = (c,)
    s[f"{p}.proj_in.weight"] = (c, c)
    s[f"{p}.proj_out.weight"] = (c, c)
    for d in range(depth):
        b = f"{p}.transformer_blocks.{d}"
        for n in ("norm1", "norm2", "norm3"):
            s[f"{b}.{n}.weight"] = (c,)
        for a, kdim in (("attn1", c), ("attn2", ctx)):
            s[f"{b}.{a}.to_q.weight"] = (c, c)
            s[f"{b}.{a}.to_k.weight"] = (c, kdim)
            s[f"{b}.{a}.to_v.weight"] = (c, kdim)
            s[f"{b}.{a}.to_out.0.weight"] = (c, c)
        s[f"{b}.ff.net.0.proj.weight"] = (8 * c, c)
        s[f"{b}.ff.net.2.weight"] = (c, 4 * c)


# ---- topology --------------------------------------------------------------
def test_every_module_is_classified_and_none_is_dropped():
    mods = sdxl.modules(mini())
    kinds = {m["kind"] for m in mods}
    assert kinds == {"mlp", "conv", "res", "transformer"}
    assert [m["name"] for m in mods][0] == "time_embed"
    assert [m["name"] for m in mods][-1] == "out"


def test_a_resblock_is_recognised_by_its_in_layers_and_reports_its_widths():
    got = {m["name"]: m for m in sdxl.modules(mini())}
    assert got["input_blocks.4.0"]["kind"] == "res"
    assert (got["input_blocks.4.0"]["cin"], got["input_blocks.4.0"]["cout"]) \
        == (64, 128)
    assert got["input_blocks.4.0"]["skip"] is True
    assert got["input_blocks.1.0"]["skip"] is False


def test_a_transformer_reports_its_depth_and_context_width():
    got = {m["name"]: m for m in sdxl.modules(mini(depth=3, ctx=512))}
    tf = got["input_blocks.4.1"]
    assert (tf["depth"], tf["ctx"], tf["c"]) == (3, 512, 128)


def test_the_resolution_halves_at_a_downsample_and_doubles_at_an_upsample():
    got = {m["name"]: m["scale"] for m in sdxl.modules(mini())}
    assert got["input_blocks.1.0"] == 1
    assert got["input_blocks.3.0"] == 1     # the stride-2 conv reads at 1
    assert got["input_blocks.4.0"] == 2     # and everything after it at 2
    assert got["output_blocks.0.2"] == 1    # the upsample conv writes at 1
    assert got["output_blocks.2.0"] == 1


def test_a_wider_checkpoint_plans_as_itself_and_not_as_the_one_this_was_written_for():
    got = {m["name"]: m for m in sdxl.modules(mini(ch=(96, 192)))}
    assert got["input_blocks.4.0"]["cout"] == 192


# ---- the plan --------------------------------------------------------------
def plan_of(**kw):
    kw.setdefault("latent", (16, 16))
    kw.setdefault("arena_words", 1 << 24)
    return sdxl.unet_plan(mini(), **kw)


def test_the_placement_is_sound():
    assert plan_of().solve().verify() == []


def test_every_skip_is_produced_on_the_way_down_and_read_on_the_way_up():
    """The first skip is live across the whole network; a planner that cannot
    see that will place something else on top of it."""
    place = plan_of().solve()
    lives = [place.life[n] for n in place.life if n.endswith(".out")
             or n.endswith(".res")]
    longest = max(l - f for f, l in lives)
    assert longest > 0.5 * len(place.plan.steps)


def test_the_skip_lifetimes_nest_like_a_stack():
    place = plan_of().solve()
    plan = place.plan
    cats = [s for s in plan.steps if s.kind == "vmap" and s.name.endswith(".cat")]
    assert len(cats) >= 3
    consumed = [place.life[s.reads[1]] for s in cats]
    starts = [f for f, _ in consumed]
    assert starts == sorted(starts, reverse=True), "skips must pop in reverse"


def test_fp16_weights_need_more_room_than_mxfp7():
    a = plan_of(preq=True).solve()
    b = plan_of(preq=False).solve()
    assert b.weight_words > a.weight_words


def test_an_arena_that_cannot_hold_the_weights_says_so_with_the_figures():
    with pytest.raises(PlanError, match="peak live activations"):
        plan_of(arena_words=1 << 12).solve()


def test_peak_activations_are_far_below_the_weights():
    """The premise of a static plan: intermediates are small next to weights,
    so there is no need to stream them."""
    place = plan_of().solve()
    assert place.peak_act_words < place.weight_words


def test_a_bigger_latent_grows_activations_and_not_weights():
    """Four times the area grows the peak by less than four: the context-sized
    and flash-block buffers do not scale with the latent."""
    small = sdxl.unet_plan(mini(), latent=(16, 16), arena_words=1 << 24).solve()
    big = sdxl.unet_plan(mini(), latent=(32, 32), arena_words=1 << 26).solve()
    assert small.weight_words == big.weight_words
    assert 2 < big.peak_act_words / small.peak_act_words < 4


def test_attention_scores_are_one_flash_block_and_not_the_whole_matrix():
    """Materialising hw x hw would be quadratic in the sequence; the plan must
    not accidentally describe that."""
    plan = plan_of(latent=(32, 32))
    scores = [t for n, t in plan.tensors.items() if n.endswith(".scores")]
    assert scores
    assert all(t.nbytes <= sdxl.Q_BLOCK * sdxl.K_BLOCK * 2 * 64 for t in scores)


def test_every_score_buffer_is_read_by_the_softmax_that_follows_it():
    place = plan_of().solve()
    assert not [w for w in place.verify() if "never read" in w]


def test_the_summary_reports_the_numbers_a_caller_needs_before_starting():
    text = sdxl.summary(plan_of().solve())
    for want in ("weights resident", "peak live acts", "weight load",
                 "one forward", "cluster GEMMs"):
        assert want in text
