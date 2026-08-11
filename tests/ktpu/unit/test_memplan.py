"""The memory plan: lifetimes, placement, and the three silent mistakes.

Every failure mode here is one the machine cannot report -- a reused address
under a live tensor, an operand image read as a drain, a read before its write --
so each one is checked against a plan built to contain exactly that mistake.
"""

import pytest

from ktpu.hw.board import MEM_WORD_BYTES
from ktpu.hw.memplan import (
    AENTRY,
    ALIGN_WORDS,
    BENTRY,
    CTILE,
    FLAT,
    Plan,
    PlanError,
)

ARENA = 1 << 20  # words


def chain(n=3, arena=ARENA):
    """`n` steps, each consuming the last step's output. The simplest reuse."""
    p = Plan(arena)
    p.input("x0", FLAT(64, 64))
    for i in range(n):
        p.act(f"x{i + 1}", FLAT(64, 64))
        p.step(f"s{i}", "vmap", reads=(f"x{i}",), writes=(f"x{i + 1}",))
    p.tensors[f"x{n}"].kind = "output"
    return p


# ---- layouts ---------------------------------------------------------------
def test_the_three_fp16_layouts_are_the_same_size_and_differ_only_in_order():
    """THIS IS WHY THE LAYOUT TAG EXISTS. All three hold m*n fp16 and occupy
    exactly m*n*2 bytes, so no size check can ever tell them apart -- only the
    element order differs, and the machine does not check it either."""
    n = 320 * 320 * 2
    assert FLAT(320, 320).nbytes == n
    assert AENTRY(320, 320).nbytes == n
    assert CTILE(320, 320).nbytes == n
    assert len({FLAT(320, 320).kind, AENTRY(320, 320).kind,
                CTILE(320, 320).kind}) == 3


def test_a_prequantised_operand_is_half_the_bytes():
    assert AENTRY(64, 64, preq=True).nbytes * 2 == AENTRY(64, 64).nbytes
    assert BENTRY(64, 64, preq=True).preq is True


@pytest.mark.parametrize("m,k", [(6, 64), (64, 48), (3, 31)])
def test_an_operand_image_that_is_not_whole_entries_is_refused(m, k):
    """Padding after placement moves every address that follows it."""
    with pytest.raises(PlanError, match="whole"):
        AENTRY(m, k)


def test_a_ctile_is_rounded_up_to_a_whole_drain_burst():
    """The DRAIN writes 8 words at a time, so the tail lands past the sub-tiles."""
    lay = CTILE(4, 4)
    assert lay.nbytes == 8 * MEM_WORD_BYTES


# ---- lifetimes -------------------------------------------------------------
def test_lifetimes_are_first_write_to_last_read():
    p = chain(3)
    life = p.lifetimes()
    assert life["x0"] == (-1, 0)
    assert life["x1"] == (0, 1)
    assert life["x2"] == (1, 2)


def test_an_output_lives_past_the_last_step_because_the_host_reads_it():
    p = chain(3)
    assert p.lifetimes()["x3"] == (2, len(p.steps))


def test_a_weight_read_by_two_steps_lives_across_both():
    p = Plan(ARENA)
    p.input("x", FLAT(16, 16))
    p.weight("w", FLAT(16, 16))
    p.act("a", FLAT(16, 16))
    p.output("b", FLAT(16, 16))
    p.step("one", "vmap", reads=("x", "w"), writes=("a",))
    p.step("two", "vmap", reads=("a", "w"), writes=("b",))
    assert p.lifetimes()["w"] == (-1, 1)


def test_reading_a_tensor_before_it_is_written_is_refused():
    p = Plan(ARENA)
    p.act("late", FLAT(16, 16))
    p.output("out", FLAT(16, 16))
    p.step("early", "vmap", reads=("late",), writes=("out",))
    with pytest.raises(PlanError, match="before anything writes it"):
        p.lifetimes()


def test_a_step_naming_an_undeclared_tensor_is_refused_at_declaration():
    p = Plan(ARENA)
    with pytest.raises(PlanError, match="not declared"):
        p.step("s", "vmap", reads=("ghost",), writes=())


def test_declaring_a_tensor_twice_is_refused():
    p = Plan(ARENA)
    p.act("a", FLAT(4, 4))
    with pytest.raises(PlanError, match="declared twice"):
        p.act("a", FLAT(8, 8))


# ---- placement -------------------------------------------------------------
def test_every_placement_verifies_clean():
    assert chain(8).solve().verify() == []
    assert chain(8).solve(reuse=False).verify() == []


def test_reuse_keeps_the_peak_flat_while_a_static_plan_grows():
    """A chain of N buffers needs two live at once however long the chain is."""
    peaks = [chain(n).solve().peak_act_words for n in (2, 8, 32)]
    assert peaks[0] == peaks[1] == peaks[2]
    grows = [chain(n).solve(reuse=False).total_words for n in (2, 8, 32)]
    assert grows[0] < grows[1] < grows[2]


def test_reuse_actually_reuses_an_address():
    """x3 lands where x1 was: x1 died at step 1 and x3 is born at step 2."""
    place = chain(4).solve()
    assert place.word["x3"] == place.word["x1"]


def test_a_step_output_never_lands_on_an_input_it_is_still_reading():
    """Allocating before freeing is what makes accidental in-place impossible."""
    place = chain(6).solve()
    for s in place.plan.steps:
        for r in s.reads:
            for w in s.writes:
                rw, rn = place.span(r)
                ww, wn = place.span(w)
                assert rw >= ww + wn or ww >= rw + rn, f"{s.name}: {r} vs {w}"


def test_weights_are_pinned_below_every_activation():
    p = chain(4)
    p.weight("w", FLAT(128, 128))
    p.steps[0].reads += ("w",)
    place = p.solve()
    wtop = place.word["w"] + place.plan.tensors["w"].words
    assert all(place.word[n] >= wtop for n in place.word if n != "w")


def test_adding_a_layer_does_not_move_a_weight_already_placed():
    """A weight uploaded over hours must not move because a later step appeared."""
    def build(nsteps):
        p = Plan(ARENA)
        p.weight("w0", FLAT(64, 64))
        p.weight("w1", FLAT(64, 64))
        p.input("x0", FLAT(64, 64))
        for i in range(nsteps):
            p.act(f"x{i + 1}", FLAT(64, 64))
            p.step(f"s{i}", "vmap", reads=(f"x{i}", "w0", "w1"),
                   writes=(f"x{i + 1}",))
        p.tensors[f"x{nsteps}"].kind = "output"
        return p.solve()

    a, b = build(2), build(9)
    assert (a.word["w0"], a.word["w1"]) == (b.word["w0"], b.word["w1"])


def test_every_address_is_drain_burst_aligned():
    place = chain(6).solve()
    assert all(w % ALIGN_WORDS == 0 for w in place.word.values())


def test_an_arena_too_small_says_what_it_would_have_needed():
    with pytest.raises(PlanError, match="peak live activations"):
        chain(3, arena=4).solve()


def test_a_computed_and_never_read_intermediate_is_reported():
    p = Plan(ARENA)
    p.input("x", FLAT(8, 8))
    p.act("dead", FLAT(8, 8))
    p.output("out", FLAT(8, 8))
    p.step("s0", "vmap", reads=("x",), writes=("dead",))
    p.step("s1", "vmap", reads=("x",), writes=("out",))
    assert any("never read" in w for w in p.solve().verify())


# ---- the overlap check itself ----------------------------------------------
def test_verify_catches_an_alias_planted_by_hand():
    """The check must be able to FAIL, or it proves nothing about the solver."""
    place = chain(4).solve()
    place.word["x2"] = place.word["x1"]
    why = place.verify()
    assert any("overlaps" in w for w in why)


def test_verify_allows_two_tensors_whose_lives_do_not_touch():
    place = chain(6).solve()
    assert place.verify() == []
    assert len({place.word[n] for n in place.word}) < len(place.word)


# ---- layout matching -------------------------------------------------------
def test_a_step_wanting_an_operand_image_refuses_a_drained_c():
    p = Plan(ARENA)
    p.input("a", AENTRY(64, 64))
    p.act("c", CTILE(64, 64))
    p.output("d", CTILE(64, 64))
    p.step("mm", "matmul", reads=("a",), writes=("c",))
    p.step("next", "matmul", reads=("c",), writes=("d",),
           wants={"c": "aentry"})
    with pytest.raises(PlanError, match="same bytes in a different order"):
        p.solve()


def test_the_same_chain_passes_once_a_repack_is_inserted():
    p = Plan(ARENA)
    p.input("a", AENTRY(64, 64))
    p.act("c", CTILE(64, 64))
    p.act("a2", AENTRY(64, 64))
    p.output("d", CTILE(64, 64))
    p.step("mm", "matmul", reads=("a",), writes=("c",))
    p.step("repack", "repack", reads=("c",), writes=("a2",),
           wants={"c": "ctile"})
    p.step("next", "matmul", reads=("a2",), writes=("d",),
           wants={"a2": "aentry"})
    assert p.solve().verify() == []


def test_wanting_a_tensor_the_step_does_not_touch_is_reported():
    p = Plan(ARENA)
    p.input("a", FLAT(8, 8))
    p.output("b", FLAT(8, 8))
    p.step("s", "vmap", reads=("a",), writes=("b",), wants={"b": "flat"})
    p.steps[0].wants["nope"] = "flat"
    assert any("neither reads nor writes" in w for w in p.check_layouts())


# ---- binding and traffic ---------------------------------------------------
def test_bind_gives_one_address_per_operand_of_a_step():
    place = chain(3).solve()
    got = place.bind(place.plan.steps[1])
    assert set(got) == {"x1", "x2"}
    assert got["x1"] == place.word["x1"]


def test_two_steps_naming_one_tensor_bind_to_the_same_address():
    """This IS the address matching; if it can differ, chaining is a lottery."""
    place = chain(4).solve()
    producer = place.bind(place.plan.steps[1])
    consumer = place.bind(place.plan.steps[2])
    assert producer["x2"] == consumer["x2"]


def test_traffic_counts_a_weight_once_and_a_step_every_time():
    p = Plan(ARENA)
    p.weight("w", FLAT(1024, 1024))
    p.input("x", FLAT(16, 16))
    p.output("y", FLAT(16, 16))
    for i in range(4):
        p.step(f"s{i}", "repack", reads=("x", "w"), writes=("y",),
               host_in=1000, host_out=2000)
    t = p.solve().traffic()
    assert t["weight_bytes"] == 1024 * 1024 * 2
    assert t["step_in_bytes"] == 4000 and t["step_out_bytes"] == 8000
    assert t["seconds"] > t["weight_seconds"]


def test_weights_can_be_priced_on_a_different_transport_from_the_steps():
    """Weights are one bulk write of a known address map and need no control
    plane, so they can go over PCIe even where dispatch cannot."""
    p = Plan(ARENA)
    p.weight("w", FLAT(1024, 1024))
    p.input("x", FLAT(16, 16))
    p.output("y", FLAT(16, 16))
    p.step("s", "repack", reads=("x", "w"), writes=("y",), host_in=1 << 20)
    place = p.solve()
    slow = place.traffic()
    fast = place.traffic(weight_bps=1_800_000_000)
    assert fast["weight_seconds"] < slow["weight_seconds"] / 1000
    assert fast["step_seconds"] == slow["step_seconds"]
    assert fast["seconds"] < slow["seconds"]


def test_byte_addresses_go_through_the_board_when_one_is_given():
    from ktpu.hw.board import Board

    place = chain(3).solve()
    board = Board.named("ship_3x2")
    assert place.byte("x1", board) == board.mem_byte(place.word["x1"])
    assert place.byte("x1") == place.word["x1"] * MEM_WORD_BYTES


def test_report_names_every_tensor_and_the_totals():
    text = chain(3).solve().report()
    assert "peak live acts" in text
    assert all(n in text for n in ("x0", "x1", "x2", "x3"))
