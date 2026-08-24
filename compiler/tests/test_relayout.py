"""A layout conversion run on a vector core instead of through the host.

Every check here is a DIGEST against the target layout's own `pack`, never a
round trip: a relayout that packs and unpacks wrongly together passes a round
trip and puts the right bytes in the wrong places, which is the failure this
whole area exists to prevent (`tests/test_layout.py`).
"""

import numpy as np
import pytest
from kohakuaccel.machinespec import STAGE_BYTES
from kohakuaccel.sim import MEM_BASE, Memory
from kohakutpu.isa import relayout as RL
from kohakutpu.isa.vecemit import entry_walk
from kohakutpu.kernels import flash_attention, mlp
from kohakutpu.model import SimDevice, VectorUnit
from kohakutpu.rt import RelayoutError

from kohakutpu import layout as LO
from kohakutpu import staging

SRC, DST, MASK = 0x1_0000, 0x8_0000, 0xF_0000

#: Every conversion the shipped kernels record, at the shapes they record it.
#: MEASURED from `plan(...).conversions`; `gn2` is not one of theirs and is here
#: because four consecutive `Tile` words are then two sub-tile ROWS.
CASES = [
    ("mlp t1", LO.Tile((2, 8), 8, 8), LO.Entry(8, 2), (64, 256)),
    ("scores 64", LO.Tile((2, 2), 8, 8), LO.Flat(), (64, 64)),
    ("scores back 64", LO.Flat(), LO.Tile((2, 2), 8, 8), (64, 64)),
    ("weights 64", LO.Flat(), LO.Entry(8, 2), (64, 64)),
    ("weights back 64", LO.Entry(8, 2), LO.Flat(), (64, 64)),
    ("scores 128", LO.Tile((4, 2), 8, 8), LO.Flat(), (128, 64)),
    ("scores 256", LO.Tile((8, 2), 8, 8), LO.Flat(), (256, 64)),
    ("scores back 256", LO.Flat(), LO.Tile((8, 2), 8, 8), (256, 64)),
    ("gn2", LO.Tile((2, 2), 8, 2), LO.Flat(), (64, 16)),
]


def walk_addresses(dims) -> list:
    """Every address `vec_agu` emits for these dims, dimension 0 fastest."""
    total = 1
    for _, bound in dims:
        total *= bound
    out = []
    for step in range(total):
        rest, addr = step, 0
        for stride, bound in dims:
            addr += (rest % bound) * stride
            rest //= bound
        out.append(addr)
    return out


def execute(before, after, shape, seed=7):
    """Run the planned conversion on the vector-core model. Returns its bytes."""
    want = np.random.default_rng(seed).standard_normal(shape).astype(np.float16)
    made = RL.plan(before, after, shape)
    assert made is not None, f"{before.key} -> {after.key} has no plan"
    mem = Memory()
    mem.write(SRC, before.pack(want))
    mem.write(MASK, LO.Flat().pack(RL.lane_groups()))
    unit = VectorUnit()
    for flit in RL.build(made).flits(MEM_BASE + SRC, MEM_BASE + DST, MEM_BASE + MASK):
        unit.execute(flit, mem)
    return mem.read(DST, made.nbytes), after.pack(want), made


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
def test_the_walked_bytes_are_the_ones_pack_would_have_written(case):
    """The whole image, against the target layout's own packer.

    A conversion that is merely plausible puts the right bytes in the wrong
    places and every unit downstream accepts them, so this compares all of it.
    """
    _, before, after, shape = case
    got, want, _ = execute(before, after, shape)
    assert got == want, f"{before.key} -> {after.key} moved the wrong bytes"


def test_a_tile_order_needs_the_lane_pass_and_the_others_do_not():
    """MEASURED: `Tile` disagrees with `Flat` and `Entry` BELOW the 32-byte word.

    A `Tile` word is a 4x4 sub-tile -- four runs of four elements from four
    different rows -- while the other two are sixteen consecutive elements of
    one row. No walk of any stride splits a word, so the granule transpose is
    not an optimisation here; it is the only thing that makes the conversion
    expressible at all.
    """
    tiled = RL.plan(LO.Tile((2, 2), 8, 8), LO.Flat(), (64, 64))
    plain = RL.plan(LO.Flat(), LO.Entry(8, 2), (64, 64))
    assert tiled.transpose, "a Tile conversion is not a word permutation"
    assert not plain.transpose, "Flat -> Entry is one, and must not pay for lanes"
    src = RL.image(LO.Tile((2, 2), 8, 8), (64, 64))
    dst = RL.image(LO.Flat(), (64, 64))
    assert RL.word_perm(src, dst) is None, "it would not need the lane pass"


@pytest.mark.parametrize(
    "rows, k, groups, blocks", [(64, 64, 8, 2), (32, 64, 8, 2), (32, 32, 8, 1)], ids=str
)
def test_the_plan_reproduces_entry_walk(rows, k, groups, blocks):
    """`entry_walk` is the pinned reference for Flat -> Entry; this must agree.

    Not field for field: the planner splits the walk into RUNs, so the check is
    that the runs COMPOSE into the same permutation `entry_walk` describes.
    """
    want = entry_walk(rows, k, groups, blocks, LO.LANES, LO.KBLOCK)
    made = RL.plan(LO.Flat(), LO.Entry(groups, blocks), (rows, k))
    assert made is not None and not made.transpose
    composed = [
        made.dst_off[r] + at
        for r in range(made.runs)
        for at in walk_addresses(made.dims)
    ]
    assert composed == walk_addresses(want)


def test_the_lane_pass_moves_lanes_and_computes_nothing():
    """A subtile relayout must be bit-exact, infinities and NaNs included.

    A predicated `VSHUF` rotates and gates a write; nothing is arithmetic. If
    the merge ever became a multiply-by-mask, `0 * inf` would put a NaN in a
    lane nobody chose.
    """
    shape = (64, 64)
    held = np.zeros(shape, np.float16)
    held[:] = np.arange(shape[1], dtype=np.float16)
    held[0, 0], held[3, 7], held[17, 33] = np.inf, -np.inf, np.nan
    before, after = LO.Tile((2, 2), 8, 8), LO.Flat()
    made = RL.plan(before, after, shape)
    mem = Memory()
    mem.write(SRC, before.pack(held))
    mem.write(MASK, LO.Flat().pack(RL.lane_groups()))
    unit = VectorUnit()
    for flit in RL.build(made).flits(MEM_BASE + SRC, MEM_BASE + DST, MEM_BASE + MASK):
        unit.execute(flit, mem)
    assert mem.read(DST, made.nbytes) == after.pack(held)
    assert unit.saturated == 0, "a store clamped, so something arithmetic happened"


def test_an_unused_dimension_is_returned_to_reset():
    """A descriptor outlives the program that wrote it.

    A kernel that states only the dimensions it uses inherits the bounds of
    whatever ran before: measured as a 128-word copy walking 4,096 entries and
    faulting F_LEN. The quiet form is a leftover walk that still fits.
    """
    made = RL.plan(LO.Flat(), LO.Entry(8, 2), (64, 64))
    kernel = RL.Strided(made)
    fields = {(f["ad"], f["fld"]) for f in _decoded(kernel.static_descs()) if f["fld"]}
    for ad in (kernel.AD_FILL, kernel.AD_DRAIN):
        assert {(ad, n) for n in range(1, 5)} <= fields, f"descriptor {ad} is partial"
    ads = (kernel.AD_FILL, kernel.AD_DRAIN)
    tail = {(f["ad"], f["fld"]) for f in _decoded(RL.reset_descs(ads))}
    assert tail == {(ad, n) for ad in ads for n in range(1, 5)}


def _decoded(flits):
    from kohakutpu.isa.vector import ISA as VEC

    out = []
    for flit in flits:
        name, f = VEC.set.decode(flit & ((1 << 256) - 1))
        if name == "DESC":
            out.append(f)
    return out


def test_a_run_covers_the_buffer_and_not_a_word_more():
    """Every run's walk is whole, and the runs tile the buffer exactly.

    A RUN stores what its descriptor walks whatever the buffer holds, so a run
    width that does not divide the total has the last one write past the end.
    """
    made = RL.plan(LO.Flat(), LO.Entry(8, 2), (64, 64))
    assert made.total % made.words == 0
    assert made.runs * made.words == made.total
    copy = RL.Copy(made.total)
    assert copy.runs * copy.w == made.total


def test_a_word_permutation_costs_no_lane_pass_at_all():
    """A VFILL lands contiguously in L1 and a VDRAIN reads contiguously from it.

    So putting the walk on one of the two descriptors performs the permutation
    outright: the image does not scale with the run and carries no VLD, VST or
    ALU word. The identity chain that moved the same bytes through the lanes was
    70 image words against these eight.
    """
    from kohakutpu.hw import vector as V

    made = RL.plan(LO.Flat(), LO.Entry(8, 2), (64, 64))
    kernel = RL.Strided(made)
    assert len(kernel.image) == RL.PLAIN_IMAGE == 8
    ops = {(w >> 27) & 0x1F for w in kernel.image}
    assert not ops & {V.OPS["VLD"], V.OPS["VST"]}, "the L1 copy is still there"


def test_the_lane_pass_covers_eight_groups_a_register():
    """`VSHUF` rotates each 16-lane chunk by the same amount, and every group of
    four words wants the same rotation -- so VL=128 moves 32 words per block.

    MEASURED: it halves the image, which at a lower clock is the larger share of
    the wall time. A run that is not whole 32-word blocks falls back to VL=64
    rather than to the host.
    """
    wide = RL.Subtile(RL.plan(LO.Tile((8, 2), 8, 8), LO.Flat(), (256, 64)))
    assert wide.chunks == 8 and wide.vl == 128 and wide.block == 32
    assert len(wide.image) <= RL.PREAMBLE_WORDS + RL.BLOCK_IMAGE * (wide.w // 32)
    assert RL.chunks_for(32) == 8 and RL.chunks_for(48) == 4


def _shuffled(pm: int, pr: int, mask, vl: int = 32, k: int = 4):
    """One VSHUF on the model, with `P[pr]` set where `mask` says."""
    from kohakutpu.hw import vector as V

    unit = VectorUnit()
    unit.vl, unit.sreg[3] = vl, k
    # The SPAN, not VL: a shuffle walks whole chunks and this has to be able to
    # see the tail it would otherwise mask.
    span = -(-vl // 16) * 16
    unit.vreg[0][:span] = np.arange(span, dtype=np.float64)
    unit.vreg[1][:span] = -1.0
    unit.preg[pr][:span] = mask
    unit.imem[0] = V.alu("VSHUF", vd=1, va=0, vb=3, pr=pr, pm=pm)
    unit.imem[1] = V.vhalt()
    unit._kernel(0, Memory())
    return unit.vreg[1][:span].copy()


def test_a_predicated_shuffle_writes_only_the_lanes_the_predicate_names():
    """Three things `vec_lanes` does that are easy to get backwards.

    The predicate is indexed by the DESTINATION lane; a lane it does not name
    KEEPS what the register held; and `pm` 2 means the complement.
    """
    want = np.arange(32, dtype=np.float64).reshape(2, 16)
    rotated = np.roll(want, -4, axis=1).reshape(-1)

    mask = np.zeros(32, bool)
    mask[4:8] = mask[20:24] = True
    got = _shuffled(pm=1, pr=1, mask=mask)
    assert np.array_equal(got[mask], rotated[mask]), "the wrong lanes were written"
    assert np.all(got[~mask] == -1.0), "an unnamed lane did not keep its value"

    flipped = _shuffled(pm=2, pr=1, mask=mask)
    assert np.all(flipped[mask] == -1.0), "pm=2 is the complement"
    assert np.array_equal(flipped[~mask], rotated[~mask])

    assert np.array_equal(_shuffled(pm=0, pr=1, mask=mask), rotated)


def test_a_shuffle_ignores_the_vl_tail_mask():
    """It writes WHOLE CHUNKS whatever VL is, unlike an ALU op.

    `VSHUF` takes the load/store write port, where the enable is the predicate
    alone -- `vec_lanes` leaves `p_tmask` out of that branch deliberately. A
    model that masked it would disagree with silicon at any VL off a multiple
    of 16, which is exactly where nobody looks.
    """
    got = _shuffled(pm=0, pr=0, mask=np.zeros(32, bool), vl=20)
    want = np.roll(np.arange(32, dtype=np.float64).reshape(2, 16), -4, axis=1)
    assert np.array_equal(got, want.reshape(-1)), "a tail mask was applied"


def test_only_a_shuffle_reads_those_bits():
    """For VLD/VST/VCVT/VBCAST the same bits are the descriptor OFFSET.

    They share the decode branch and the core forces `pm=0` for them, so a
    model that read `pm` off any of the four would predicate a load by accident.
    """
    from kohakutpu.hw import vector as V

    unit = VectorUnit()
    unit.vl = 16
    unit.preg[1][:16] = False
    unit.l1[10] = np.frombuffer(np.full(16, 3.0, np.float16).tobytes(), np.uint8)
    # Offset 10 sets exactly the bits VSHUF reads as pr=1, pm=1.
    word = V.vld(2, 0, 10)
    assert (word >> 3) & 3 == 1 and (word >> 1) & 3 == 1
    unit.imem[0], unit.imem[1] = word, V.vhalt()
    unit.dbase[0], unit.dbound[0][0] = 0, 1
    unit._kernel(0, Memory())
    assert np.all(unit.vreg[2][:16] == 3.0), "a VLD was predicated away"


def test_the_merge_is_a_predicate_and_not_an_instruction():
    """`VSHUF` became predicable, so the twelve `VSEL` a block spent are gone.

    Four loads, four rotates per output word, four stores: 24 instructions per
    32 words against 36. Checked as OPCODES rather than as a count, because a
    count would pass if the merge came back under another name.
    """
    from kohakutpu.hw import vector as V

    made = RL.plan(LO.Tile((8, 2), 8, 8), LO.Flat(), (256, 64))
    kernel = RL.Subtile(made)
    ops = [(w >> 27) & 0x1F for w in kernel.image]
    assert V.OPS["VSEL"] not in ops, "the merge is still an instruction"
    blocks = kernel.w // kernel.block
    assert ops.count(V.OPS["VSHUF"]) == 16 * blocks
    assert ops.count(V.OPS["VLD"]) == 4 * blocks + 1, "the lane-index read too"
    assert ops.count(V.OPS["VST"]) == 4 * blocks
    # Three predicates, one per granule that is NOT the unconditional first.
    assert ops.count(V.OPS["VCMPEQ"]) == 3


@pytest.mark.parametrize("attach", [False, True], ids=["dram", "l2"])
def test_every_conversion_runs_on_the_card(attach):
    """The headline: every conversion `mlp` and `flash_attention` record.

    Both staging tiers, because a DRAM staging span and an L2 one are different
    addresses through different decoders and only one of them has ever been
    exercised.

    NO HOST BASELINE to compare against any more, and that is the point. This
    used to run the same calls with `device_relayout=False` and assert the host
    counter was positive -- measuring a path that no longer exists. `convert`
    does not read that flag at all: it is on-card unconditionally and RAISES
    when it has no walk, so `relayouts` can only ever be 0.
    """
    for build in (_mlp_call, _attn_call):
        card = _counters(build, oncard=True, attach=attach)
        assert card["relayouts_device"] > 0, "this call records no conversion at all"
        assert card["relayouts"] == 0, "a conversion went through the host"


def _counters(build, oncard: bool, attach: bool) -> dict:
    dev = SimDevice()
    dev.device_relayout = oncard
    if attach:
        staging.attach(dev)
    build(dev)
    return {"relayouts_device": 0, **dev.counters}


def _mlp_call(dev):
    rng = np.random.default_rng(0)
    return mlp(
        dev.tensor(rng.standard_normal((64, 128)).astype(np.float16)),
        dev.tensor((rng.standard_normal((256, 128)) * 0.1).astype(np.float16)),
        dev.tensor((rng.standard_normal((64, 256)) * 0.1).astype(np.float16)),
    ).numpy()


def _attn_call(dev):
    rng = np.random.default_rng(1)
    q, k = (rng.standard_normal((4, 128, 64)).astype(np.float16) for _ in "qk")
    v = rng.standard_normal((4, 64, 128)).astype(np.float16)
    return flash_attention(dev.tensor(q), dev.tensor(k), dev.tensor(v)).numpy()


def test_the_numbers_do_not_move_when_the_relayout_does():
    """A permutation is a permutation. The two paths must agree BIT for BIT.

    Not a tolerance: the host repack and the vector pass move the same bytes, so
    any difference at all is one of them being wrong.
    """
    host = _mlp_call(_plain(SimDevice()))
    card = _mlp_call(staging.attach(SimDevice()))
    assert np.array_equal(host, card)


def _plain(dev):
    dev.device_relayout = False
    return dev


def test_a_conversion_with_no_walk_is_refused():
    """It has to FAIL, not fall back: there is no host path to fall back to.

    This asserted the opposite -- that the conversion happened through the host
    and bumped the host counter. That path is gone by decision, so the only
    correct behaviour left is a refusal loud enough to reach the caller.
    """
    dev = _plain(SimDevice())
    before, after = LO.Flat(), LO.Entry(8, 2)
    # 24 columns is not whole K-blocks, so the two orders are different SIZES
    # and no permutation between them exists.
    assert RL.plan(before, after, (8, 24)) is None
    want = np.random.default_rng(2).standard_normal((8, 24)).astype(np.float16)
    addr = dev.put(want, before).addr
    with pytest.raises(RelayoutError, match="not a walk this machine has"):
        dev.convert(addr, (8, 24), before, after)
    assert dev.counters["relayouts"] == 0
    assert dev.counters.get("relayouts_device", 0) == 0


def test_a_staged_temp_carries_the_aperture_and_a_plain_one_does_not():
    """`L.temp(tier="l2")` has to form an address `mag_stage` decodes.

    Bit 39 set, bit 38 clear, the mesh in [37:36] and the aperture in [35:32].
    Without a staging arena the same kernel places it in DRAM and runs, which is
    what lets a kernel name a tier a machine may not carry.
    """
    from kohakuaccel.machinespec import AP_STAGE, MachineSpec

    dev = staging.attach(SimDevice())
    span = dev.alloc(4096, "l2")
    assert MachineSpec.addr_aperture(span) == AP_STAGE
    assert MachineSpec.addr_mesh(span) == dev.machine.default
    dev.free(span)

    plain = SimDevice()
    assert MachineSpec.addr_aperture(plain.alloc(4096, "l2")) is None


#: MEASURED: every conversion `mlp` and `flash_attention` record permutes only
#: each run's own words, so it needs no staging at all. Whether one does is a
#: property of the SHAPE as much as the pair -- `entry:8x2 -> flat` is in place
#: at (64, 64) and moves words between runs at (64, 128).
STAGED = (LO.Entry(8, 2), LO.Flat(), (64, 128))


def test_a_walk_that_crosses_its_runs_goes_through_staging_and_comes_home():
    """The two-pass path, which the shipped kernels do not reach.

    A permutation is performable in place only when each run permutes its OWN
    words -- the fill has them all in L1 behind a VBAR before the drain starts.
    A re-tiling moves words between runs, so run 0 would overwrite what run 3
    has still to read, and it has to land somewhere else first.
    """
    before, after, shape = STAGED
    made = RL.plan(before, after, shape)
    assert made is not None and not made.inplace
    dev = staging.attach(SimDevice())
    want = np.random.default_rng(4).standard_normal(shape).astype(np.float16)
    addr = dev.put(want, before).addr
    dev.convert(addr, shape, before, after)
    assert dev.counters["relayouts_device"] == 1
    assert dev.counters["relayouts"] == 0
    assert dev.read(addr, after.nbytes(shape)) == after.pack(want)
    assert dev.staging.used == 0, "the staging span was not returned"


def test_the_shipped_conversions_need_no_staging_at_all():
    """Measured, and it is why the relayout costs one pass rather than two."""
    for before, after, shape in [
        (LO.Tile((2, 8), 8, 8), LO.Entry(8, 2), (64, 256)),
        (LO.Tile((8, 2), 8, 8), LO.Flat(), (256, 64)),
        (LO.Flat(), LO.Tile((8, 2), 8, 8), (256, 64)),
        (LO.Flat(), LO.Entry(8, 2), (64, 64)),
        (LO.Entry(8, 2), LO.Flat(), (64, 64)),
    ]:
        assert RL.plan(before, after, shape).inplace, f"{before.key} -> {after.key}"


def test_the_cost_model_charges_a_conversion_what_the_simulator_spends():
    """Two independent routes to one number, which is this project's standard.

    `kohakutpu.cost` walks the image it would emit; `SimMachine.busy` counts what
    the vector model executed. A cost model that cannot see a relayout reports
    identical cycles with and without one, and would let it back in unnoticed.
    """
    from kohakuaccel.rt import Runtime

    from kohakutpu import cost as C

    spent = []
    orig = Runtime.dispatch

    def watch(self, payloads, unit, name="kernel", nodes=None, acks=None):
        was = sum(self.transport.busy.values())
        out = orig(self, payloads, unit, name, nodes, acks)
        if name.startswith("relayout"):
            spent.append(sum(self.transport.busy.values()) - was)
        return out

    Runtime.dispatch = watch
    try:
        dev = SimDevice()
        rng = np.random.default_rng(0)
        args = (
            dev.tensor(rng.standard_normal((64, 128)).astype(np.float16)),
            dev.tensor((rng.standard_normal((256, 128)) * 0.1).astype(np.float16)),
            dev.tensor((rng.standard_normal((64, 256)) * 0.1).astype(np.float16)),
        )
        held = mlp.plan(*args)
        analytic = C.time(held)
        mlp(*args).numpy()
    finally:
        Runtime.dispatch = orig

    priced = sum(v for k, v in analytic.by_kind.items() if k.startswith("relayout"))
    assert priced == sum(spent), "the model and the machine disagree"
    assert priced > 0, "the conversion was charged nothing"

    held.conversions, held._touch = [], None
    assert C.time(held).cycles < analytic.cycles, "the model cannot see it"


def test_a_conversion_the_machine_cannot_walk_is_charged_no_cycles():
    """It costs a HOST round trip, which is not cycles on this machine at all.

    Charging it zero and NAMING it is the honest form; charging it a made-up
    number would put a figure in a table that no measurement backs.
    """
    from kohakutpu import cost as C

    dev = SimDevice()
    held = mlp.plan(
        dev.tensor(np.zeros((64, 128), np.float16)),
        dev.tensor(np.zeros((256, 128), np.float16)),
        dev.tensor(np.zeros((64, 256), np.float16)),
    )
    at, name, before, _ = held.conversions[0]
    held.conversions = [(at, name, before, LO.Entry(8, 3))]
    held._touch = None
    assert RL.for_conversion(before, LO.Entry(8, 3), held.shape(name)) is None
    assert "relayout:host" in C.time(held).by_kind


def test_a_converted_temp_is_proposed_for_the_staging_store():
    """The temps whose byte order CHANGES are the ones that want to be in `S`.

    A conversion pays a ragged access on whichever side carries the walk, and
    `S` is where irregular access is free. Once the buffer lives there, every
    later conversion of it is `S -> S` and needs no staging pass at all:
    MEASURED 32.0 credits a byte against 2.0, and the same flit count.
    """
    from kohakutpu import cost as C

    dev = staging.attach(SimDevice())
    rng = np.random.default_rng(1)
    q, k = (rng.standard_normal((4, 128, 64)).astype(np.float16) for _ in "qk")
    v = rng.standard_normal((4, 64, 128)).astype(np.float16)
    args = (dev.tensor(q), dev.tensor(k), dev.tensor(v))
    held = flash_attention.plan(*args)

    converted = {name for _, name, _, _ in held.conversions}
    assert converted, "this shape records no conversion"
    assert set(held.tiers) == converted, "a converted temp was left in DRAM"

    seen = []
    plain = dev.alloc
    dev.alloc = lambda n, tier=None: seen.append(tier) or plain(n, tier)
    out = np.asarray(flash_attention(*args).numpy())
    dev.alloc = plain
    assert seen.count("l2") == len(converted)
    assert dev.counters.get("staging_full", 0) == 0

    lean = C.credits(held, room=1 << 21)
    fat = C.credits(held, room=0)
    assert lean["total"] * 8 < fat["total"], "the tier bought nothing"

    bare = SimDevice()
    again = (bare.tensor(q), bare.tensor(k), bare.tensor(v))
    assert np.array_equal(out, np.asarray(flash_attention(*again).numpy()))


def test_staging_the_walk_is_cheaper_in_credits_than_walking_in_place():
    """`docs/notes/data-movement-problem.md` §5, and it INVERTS the pass count.

    Walking over the buffer is one pass and puts a non-sequential access on slow
    memory at 30 credits a byte; staging in `S` is two passes and does not. The
    doc's own guidance is that an extra local pass to avoid a ragged slow-memory
    access is almost always right, and here it is 32 credits against 6.
    """
    from kohakutpu import cost as C

    made = RL.plan(LO.Tile((8, 2), 8, 8), LO.Flat(), (256, 64))
    assert made.inplace and made.mode == "drain"
    n = made.nbytes
    over = sum(c for _, c in C.relayout_moves(made, 1, None))
    staged = sum(c for _, c in C.relayout_moves(made, 1, "S"))
    assert over == 32 * n and staged == 6 * n
    assert C.route_for(made, room=0) is None, "with no S there is nowhere to stage"
    assert C.route_for(made, room=n) == "S"


def test_a_gather_gains_nothing_from_staging():
    """Staging absorbs the walk only when the walk is on the DRAIN.

    A `fill` mode plan reads the buffer ragged whatever it writes to, so staging
    buys a pass and removes no slow-memory penalty. The router has to know that
    or it spends a pass for nothing.
    """
    from kohakutpu import cost as C

    made = RL.plan(LO.Flat(), LO.Tile((8, 2), 8, 8), (256, 64))
    assert made.mode == "fill"
    over = sum(c for _, c in C.relayout_moves(made, 1, None))
    staged = sum(c for _, c in C.relayout_moves(made, 1, "S"))
    assert staged > over, "staging a gather made it look cheaper"
    assert C.route_for(made, room=made.nbytes) is None


def test_the_shard_axis_test_predicts_zero_link_credits():
    """§7 question 3, answered exactly rather than by search.

    A sharding is a partition into contiguous blocks; the layout change
    preserves it iff no word leaves its own block. That predicts ZERO link
    credits, which are the most expensive ones there are.
    """
    local = RL.plan(LO.Tile((8, 2), 8, 8), LO.Flat(), (256, 64))
    assert RL.shard_local(local, 4) and RL.crossing(local, 4) == 0

    # In place is NOT the same property: a run may permute within itself and
    # still cross a shard boundary when the block is NARROWER than the run.
    wide = RL.plan(LO.Entry(2, 2), LO.Entry(8, 2), (64, 128))
    assert wide.inplace and wide.words == 256
    assert not RL.shard_local(wide, 4), "a 128-word block inside a 256-word run"
    assert RL.crossing(wide, 4) > 0
    assert RL.shard_local(wide, 2), "at 2 ways the block IS a whole run"


@pytest.mark.parametrize(
    "shape, grid",
    [((4, 128, 64), (4, 2)), ((4, 64, 128), (2, 4))],
    ids=["qk", "v"],
)
def test_a_tensor_handed_between_two_kernels_is_reordered_on_the_card(shape, grid):
    """The CROSS-kernel relayout, which `Compiled.conversions` never sees.

    One kernel drains this in `Tile` order and the next fills it as `Entry`.
    The two cannot be made to agree: a cluster drains sub-tiles and fills
    entries, and that is silicon. It happens in `Tensor.address`, between two
    calls, so the intra-kernel executor never reached it.

    Out of place and therefore ONE pass -- two different buffers, so no run can
    write a word a later run still has to read.
    """
    from kohakuaccel.memory import Batched

    block = shape[1:]
    before = Batched(LO.Tile(grid, 8, 8), shape[0], block)
    after = Batched(LO.Entry(8, 2), shape[0], block)
    want = np.random.default_rng(5).standard_normal(shape).astype(np.float16)

    dev = SimDevice()
    held = dev.tensor(want)
    first = held.address(before)
    # What a kernel leaves behind: the bytes are on the card and nowhere else.
    held.host = None
    second = held.address(after)

    assert second != first, "it reordered over the buffer it was handed"
    assert dev.read(second, after.nbytes(shape)) == after.pack(want)
    assert dev.counters["relayouts"] == 0
    assert dev.counters["relayouts_device"] == 1


def test_contents_the_host_already_has_are_packed_rather_than_walked():
    """One upload beats a read and a walk, so the card path must not take it.

    `relayouts` only ever counted a HOST ROUND TRIP, and a tensor whose contents
    are still on the host was never one.
    """
    dev = SimDevice()
    want = np.random.default_rng(6).standard_normal((64, 64)).astype(np.float16)
    held = dev.tensor(want)
    held.address(LO.Tile((2, 2), 8, 8))
    held.address(LO.Entry(8, 2))
    assert dev.counters["relayouts"] == 0
    assert dev.counters.get("relayouts_device", 0) == 0


def _flash_plan(dev, heads, lq, lkv):
    z = np.zeros
    return flash_attention.plan(
        dev.tensor(z((heads, lq, 64), np.float16)),
        dev.tensor(z((heads, lkv, 64), np.float16)),
        dev.tensor(z((heads, 64, lkv), np.float16)),
        block=64,
    )


@pytest.mark.parametrize("lkv, blocks", [(128, 2), (256, 4), (512, 8)], ids=str)
def test_a_conversion_the_next_stage_overwrites_is_not_run(lkv, blocks):
    """`3*blocks - 3` of `flash_attention`'s `6*blocks - 3` move dead bytes.

    Its temps are reused across key blocks, so after one block reads `scores` as
    flat the next block's DRAIN rewrites the whole buffer as tile. A conversion
    is derived from the sequence of USES and never asked whether the old order
    was still live.
    """
    held = _flash_plan(SimDevice(size=8192 << 20), 4, 256, lkv)
    assert len(held.conversions) == 6 * blocks - 3
    assert len(held.dead) == 3 * blocks - 3
    ran = [c for at in range(len(held.stages)) for c in held.before(at)]
    assert len(ran) == len(held.conversions) - len(held.dead)


def test_dropping_them_changes_no_number_at_all():
    """It must be inert: the bytes it declines to move are overwritten anyway.

    Run twice on one compilation, once with the dead set restored, and compare
    BIT for bit -- a tolerance would not tell a dropped conversion from a
    wrongly dropped one.
    """
    rng = np.random.default_rng(1)
    q, k = (rng.standard_normal((4, 128, 64)).astype(np.float16) for _ in "qk")
    v = rng.standard_normal((4, 64, 128)).astype(np.float16)

    def go(revive):
        dev = SimDevice()
        args = (dev.tensor(q), dev.tensor(k), dev.tensor(v))
        held = flash_attention.plan(*args)
        saved = set(held.dead)
        assert saved, "this shape has no dead conversion to test with"
        if revive:
            held.dead, held._touch = set(), None
        try:
            return np.asarray(flash_attention(*args).numpy()), len(saved)
        finally:
            held.dead, held._touch = saved, None

    lean, n = go(False)
    full, _ = go(True)
    assert np.array_equal(lean, full), f"{n} dropped conversions changed the answer"


def test_a_write_that_does_not_cover_the_buffer_keeps_its_conversion():
    """The sound rule is whole-buffer, not merely written.

    A partial write leaves the rest in the OLD order for something later to read,
    so the coverage is proved from the statements rather than assumed from the
    name. `_whole` is that proof and it must refuse a gap.
    """
    from kohakutpu.lang.backend import _whole

    assert _whole([(0, 64), (64, 128)], 128)
    assert _whole([(0, 128)], 128)
    assert not _whole([(0, 64), (72, 128)], 128), "a gap was called covered"
    assert not _whole([(0, 64)], 128), "a short write was called covered"
    assert not _whole([], 128)


def test_a_kernel_can_put_its_intermediate_in_l2_and_still_run_without_one():
    """`L.temp(tier="l2")` through the whole path: trace, place, FILL, DRAIN.

    The cluster reaches the store BY ADDRESS -- there is no instruction for it --
    so what this checks is that the address a FILL carries has bit 39 set and
    that the same kernel on a machine with no staging arena runs in DRAM and
    returns the same numbers.
    """
    from kohakuaccel.lang import dims, loop, units
    from kohakuaccel.machinespec import AP_STAGE, MachineSpec
    from kohakutpu.lang import kernel

    from kohakutpu import lang as L

    M, K, N, F = dims("M, K, N, F")

    @kernel
    def staged(
        x=L.In(..., M, K),
        up=L.In(F, K),
        down=L.In(N, F),
        y=L.Out(..., M, N),
        *,
        gm=8,
        gn=8,
        nk=2,
    ):
        h = L.temp(M, F, tier="l2")
        with units(x.tiles(gm), up.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                acc += x[i, k] @ up[j, k]
            h[i, j] <<= acc
        with units(h.tiles(gm), down.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for k in loop(h.chunks32(nk)):
                acc += h[i, k] @ down[j, k]
            y[i, j] <<= acc

    def go(dev):
        rng = np.random.default_rng(0)
        args = [
            dev.tensor(rng.standard_normal((64, 128)).astype(np.float16)),
            dev.tensor((rng.standard_normal((256, 128)) * 0.1).astype(np.float16)),
            dev.tensor((rng.standard_normal((64, 256)) * 0.1).astype(np.float16)),
        ]
        assert staged.plan(*args).tiers == {"t1": "l2"}
        seen, held = [], dev.alloc
        dev.alloc = lambda n, tier=None: seen.append(tier) or held(n, tier)
        out = staged(*args).numpy()
        return out, seen

    on, tiers = go(staging.attach(SimDevice()))
    assert "l2" in tiers
    off, _ = go(SimDevice())
    assert np.array_equal(on, off), "where the temp lives changed the answer"

    dev = staging.attach(SimDevice())
    assert MachineSpec.addr_aperture(dev.alloc(4096, "l2")) == AP_STAGE


def test_the_staging_store_is_the_size_the_bitstream_backs():
    """2 MB per mesh: STAGE_BANKS 4 x STAGE_ENTRIES 16,384 x 256 b.

    An offset past it WRAPS onto another entry rather than faulting, so the
    arena has to be the bound rather than the hardware.
    """
    from kohakuaccel.machinespec import MachineSpec
    from kohakuaccel.memory import OutOfMemory

    dev = staging.attach(SimDevice())
    assert dev.staging.size == STAGE_BYTES == 1 << 21
    with pytest.raises(OutOfMemory):
        dev.staging.alloc(STAGE_BYTES + LO.WORD_BYTES)

    # A tier is a PLACEMENT: too big for the store lands in DRAM and says so,
    # because a store that quietly stopped being used is an invisible cliff.
    span = dev.alloc(STAGE_BYTES + LO.WORD_BYTES, "l2")
    assert MachineSpec.addr_aperture(span) is None
    assert dev.counters["staging_full"] == 1
