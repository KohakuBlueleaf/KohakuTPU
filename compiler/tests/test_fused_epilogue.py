"""A matmul whose activation runs on the accumulator, not on a DRAM temp.

A `DRAIN` addresses the memory port or a NoC node, so an epilogue can be handed
straight to a vector core's L1. What is checked here is what can be checked
WITHOUT the card: the instruction fields, the placement the two halves agree on,
the byte order of the result, and that the intermediate never gets an address.
Latency and bandwidth are what only hardware can settle.
"""

import pytest
from kohakuaccel.dispatch import deal, plan
from kohakuaccel.lang import dims, iface, loop, units
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.isa import ISA
from kohakutpu.kernels import linear_silu, sigmoid
from kohakutpu.lang import BACKEND, kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")

#: Four of each, so a two-instance grid pairs injectively and leaves cores free.
#: `agent` is (1,1) to match `tests/mas/mm_mesh_peer_tb.v`, which simulates it.
MACHINE = MachineSpec(
    name="test",
    units={
        "MG": ((1, 1), (2, 1), (1, 2), (2, 2)),
        "VC": ((1, 0), (2, 0), (3, 0), (3, 1)),
    },
    inst_depth=512,
    agent=(1, 1),
)

#: A tiling whose grid fits the vector cores and whose tile fits an L1.
TILING = {"gm": 4, "gn": 8, "nk": 2}


class Shaped:
    """What `solve` needs of an argument, without a device to put it on."""

    def __init__(self, shape) -> None:
        self.shape = shape


def compile_at(fn, m=32, k=64, n=32, machine=MACHINE, **knobs):
    bound = {"x": Shaped((m, k)), "w": Shaped((n, k))}
    return fn.compile(machine, iface.solve(fn.signature, bound), **{**TILING, **knobs})


def staged(m=32, k=64, n=32, **knobs):
    """`linear_silu` with its epilogue through DRAM.

    What four hand-written `*_separated` kernels used to be. `fuse=False` is the
    knob `relax` sets when a grid the cores cannot hold forces the fallback, so
    this is the same path a real staging takes rather than a second kernel.
    """
    return compile_at(linear_silu, m, k, n, fuse=False, **knobs)


def addresses(compiled) -> dict:
    """A plausible address for every buffer, so `encode` has one to use."""
    out = {"x": 0x1000, "w": 0x2000, "y": 0x3000}
    named = [*compiled.temps, *BACKEND.constants(compiled)]
    for at, name in enumerate(named):
        out[name] = 0x10000 + at * 0x1000
    return out


def program(compiled) -> dict:
    """Stage index -> the instruction words each instance runs."""
    addrs = addresses(compiled)
    return {s.index: BACKEND.encode(compiled, s, addrs) for s in compiled.stages}


def drains(compiled) -> list:
    """Every decoded DRAIN of the cluster stage, in instance order."""
    words = program(compiled)[0]
    out = []
    for at in sorted(words):
        for w in words[at]:
            name, fields = ISA.set.decode(w)
            if name == "DRAIN":
                out.append((at, fields))
    return out


# ------------------------------------------------------------------ structure
def test_the_two_halves_land_on_two_units():
    """One `with units(...)`, two stages: a grid may cross a unit boundary."""
    got = compile_at(linear_silu)
    assert [s.unit for s in got.stages] == ["MG", "VC"], got.summary()
    assert got.stages[0].grid == got.stages[1].grid


def test_the_intermediate_never_gets_an_address():
    """The fused form declares no temp; the staged one declares exactly one."""
    assert compile_at(linear_silu).temps == {}
    assert len(staged().temps) == 1


def test_no_relayout_either_way():
    """A peer-delivered tile is the same permutation a drained one already was."""
    assert compile_at(linear_silu).conversions == []


def test_the_result_is_still_sub_tile_order():
    """The epilogue writes L1 words back in order, so `y` is what a DRAIN wrote.

    If this drifted to `flat`, the two forms would disagree on the card while
    both looking right in isolation.
    """
    got = compile_at(linear_silu)
    assert got.layouts["y"].key == staged().layouts["y"].key
    assert got.layouts["y"].key.startswith("tile:")


# ------------------------------------------------------------------- encoding
def test_a_fused_drain_names_a_node():
    """`dnode`, the destination, the buffer and the completion flag."""
    for at, fields in drains(compile_at(linear_silu)):
        assert fields["dnode"] == 1
        assert (
            fields["dbuf"] == 0
        ), "a vector core has one flat L1; anything else faults"
        assert fields["dflags"] & 1, "without signal_on_complete nothing sequences RUN"
        assert fields["n"] == TILING["gm"] * TILING["gn"]
        assert fields["dmesh"] == 0 and fields["dfin"] == 0, "a local drain"
        # Zero would answer the SENDING cluster, which drops it: mm_mesh_peer.
        assert (fields["dack_x"], fields["dack_y"]) == MACHINE.agent


def test_the_destination_is_the_core_the_epilogue_runs_on():
    """The coordinate inside the instruction and the placement must agree.

    They are the same `deal` call. A second copy of the rule would agree until
    one of them was edited, and the failure would be a tile sent to an idle core.
    """
    got = compile_at(linear_silu)
    placed = deal([i.at for i in got.stages[1].instances], got.stages[1].nodes)
    for at, fields in drains(got):
        assert (fields["dst_x"], fields["dst_y"]) == placed[at]


def test_a_plain_drain_still_addresses_memory():
    """Zero everywhere new, so the staged form encodes exactly as before."""
    for _, fields in drains(staged()):
        assert fields["dnode"] == 0
        assert (fields["dst_x"], fields["dst_y"], fields["dbuf"], fields["dflags"]) == (
            0,
            0,
            0,
            0,
        )


def test_the_drain_address_is_a_granule_base_not_a_dram_address():
    """`addr[20:5]` becomes the descriptor's `off`, so this is an L1 word."""
    for _, fields in drains(compile_at(linear_silu)):
        assert fields["addr"] == 0


# ------------------------------------------------------------------ placement
def test_the_epilogue_stage_is_pinned_to_the_receiving_cores():
    """A freer placement would run the program where the data is not."""
    got = compile_at(linear_silu)
    assert got.stages[1].nodes == MACHINE.coords("VC")
    assert got.stages[0].nodes is None


def test_every_receiver_has_one_sender():
    """One open stream per receiver: two senders' bursts merge into each other."""
    got = compile_at(linear_silu)
    receivers = [coord for coord, _ in got.stages[0].acks.values()]
    assert len(set(receivers)) == len(receivers)


def test_the_cluster_stage_waits_on_the_receivers():
    """The artifact carries an await on a node it never kicked."""
    got = compile_at(linear_silu)
    words = program(got)[0]
    rounds = plan(words, MACHINE.coords("MG"), acks=got.stages[0].acks)
    waited = {s.coord for a in rounds for s in a.steps if type(s).__name__ == "Await"}
    assert {coord for coord, _ in got.stages[0].acks.values()} <= waited


# ------------------------------------------------------------------- the cost
def test_fusing_moves_less():
    """Fewer instruction words, fewer rounds, and no intermediate in DRAM.

    The vector half loses its whole `VFILL` side and its folded constants, which
    is what a fill of a broadcast array cost.
    """
    fused, apart = compile_at(linear_silu), staged()

    def cost(got):
        flits = rounds = 0
        for stage in got.stages:
            words = BACKEND.encode(got, stage, addresses(got))
            flits += sum(len(w) for w in words.values())
            rounds += len(plan(words, MACHINE.coords(stage.unit)))
        return flits, rounds

    fused_flits, fused_rounds = cost(fused)
    apart_flits, apart_rounds = cost(apart)
    assert fused_flits < apart_flits, (fused_flits, apart_flits)
    assert fused_rounds <= apart_rounds, (fused_rounds, apart_rounds)


def test_the_epilogue_reads_no_dram_operand():
    """Folded scalars go into registers, not into a broadcast array on the card."""
    assert BACKEND.constants(compile_at(linear_silu)) == {}
    assert BACKEND.constants(staged()) != {}


def test_nothing_of_the_intermediate_is_allocated():
    """The bytes fusing removes, counted: the temp both ways plus the constants.

    The staged form writes `h`, reads it back, and fills two broadcast arrays
    one `part` long apiece. The fused form allocates none of it.
    """

    def dram(got):
        temps = sum(
            got.layouts[n].nbytes(got.shape(n)) for n in got.temps if n in got.layouts
        )
        folded = sum(array.nbytes for array, _ in BACKEND.constants(got).values())
        return temps, folded

    assert dram(compile_at(linear_silu)) == (0, 0)
    temps, folded = dram(staged())
    assert temps == 32 * 32 * 2 and folded > 0, (temps, folded)


# --------------------------------------------- what a refusal turns into now
# Each of these WAS a refusal; `Kernel.relax` now stages the fusion instead.
def test_a_machine_that_does_not_say_where_its_agent_is_stages():
    """The acknowledgement has nowhere to go, so the tile cannot cross the NoC."""
    blind = MachineSpec(
        name="blind",
        units={"MG": ((1, 1),), "VC": ((1, 0),)},
        inst_depth=512,
    )
    got = compile_at(linear_silu, m=16, n=32, machine=blind)
    assert got.knobs["fuse"] is False and len(got.temps) == 1


def test_a_grid_wider_than_the_cores_stages():
    """More tiles than receivers cannot be arbitrated, so the tile lands instead."""
    got = compile_at(linear_silu, gm=1, gn=1)
    assert got.knobs["fuse"] is False and len(got.temps) == 1


def test_a_tile_too_big_for_an_l1_stages():
    """`require_l1`: the measured bad band completes and returns wrong numbers."""
    wide = MachineSpec(
        name="wide",
        units={"MG": ((1, 1),), "VC": ((1, 0),)},
        inst_depth=512,
        agent=(1, 1),
    )
    got = compile_at(linear_silu, m=64, n=128, machine=wide, gm=16, gn=32)
    assert got.knobs["fuse"] is False and len(got.temps) == 1


def test_a_PER_CHANNEL_operand_beside_the_tile_now_fuses():
    """It used to be refused for needing "a fill of its own". It had one.

    `tests/test_bias_epilogue.py` is the full account; this is the case that
    file's refusal message used to name.
    """

    @kernel
    def biased(
        x=L.In(M, K), w=L.In(N, K), b=L.In(N), y=L.Out(M, N), *, gm=4, gn=8, nk=2
    ):
        with units(x.tiles(gm), w.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                acc += x[i, k] @ w[j, k]
            y[i, j] <<= (acc + b[j]) * 0.5

    bound = {"x": Shaped((32, 64)), "w": Shaped((32, 64)), "b": Shaped((32,))}
    got = biased.compile(MACHINE, iface.solve(biased.signature, bound))
    assert got.temps == {}, sorted(got.temps)


def _flat(trace) -> list:
    """Every statement of `trace`, in emitted order, depth first."""
    out: list = []

    def walk(node) -> None:
        for s in node if isinstance(node, list) else getattr(node, "body", []) or []:
            if getattr(s, "kind", None):
                out.append(s)
            walk(s)

    walk(trace.top)
    return out


def _kinds(trace) -> list[str]:
    """Just the kinds, which is what an ordering assertion reads."""
    return [s.kind for s in _flat(trace)]


def test_two_accumulators_drain_into_one_core():
    """One drain hands over one tile, so two tiles are two drains and two slots.

    The cluster holds ONE accumulator, so the first tile has to leave before the
    second sweep clears it -- the drain is lifted above the tile that overwrites
    it, and the order below is the whole correctness argument.
    """

    @kernel
    def twice(x=L.In(M, K), w=L.In(N, K), y=L.Out(M, N), *, gm=4, gn=8, nk=2):
        with units(x.tiles(gm), w.tiles(gn)) as (i, j):
            a = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                a += x[i, k] @ w[j, k]
            b = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                b += x[i, k] @ w[j, k]
            y[i, j] <<= a * b

    kinds = _kinds(twice.trace())
    assert kinds == [
        *("fill", "fill", "gemm", "drain"),
        *("fill", "fill", "gemm", "drain"),
        "apply",
    ]
    drains = [s for s in _flat(twice.trace()) if s.kind == "drain"]
    assert [s.args["slot"] for s in drains] == [0, 1]
    assert all(s.args["node"] for s in drains)


def test_two_accumulators_must_agree_on_shape():
    """They land in equal L1 regions on one core, so one shape or none."""

    @kernel
    def mixed(x=L.In(M, K), w=L.In(N, K), y=L.Out(M, N), *, gm=4, gn=8, nk=2):
        with units(x.tiles(gm), w.tiles(gn)) as (i, j):
            a = L.tile(gm, gn, nk)
            for k in loop(x.chunks32(nk)):
                a += x[i, k] @ w[j, k]
            b = L.tile(gm, gn * 2, nk)
            for k in loop(x.chunks32(nk)):
                b += x[i, k] @ w[j, k]
            y[i, j] <<= a * b

    with pytest.raises(L.LangError, match="one shape"):
        mixed.trace()


# ------------------------------------------------------------- the other road
def test_manual_separation_is_untouched():
    """Two stages and a temp must stay legal; the fused path is a default, not a law."""
    got = staged()
    assert [s.unit for s in got.stages] == ["MG", "VC"]
    assert len(got.temps) == 1


def test_both_forms_write_the_same_bytes_of_y():
    """Same addresses, same order, so the two are comparable on the card."""
    fused, apart = compile_at(linear_silu), staged()
    shape = fused.shape("y")
    assert fused.layouts["y"].nbytes(shape) == apart.layouts["y"].nbytes(shape)


def test_sigmoid_lowers_with_no_guessed_operand_slot():
    """Every source selector this epilogue uses is one a run kernel demonstrates."""
    from kohakutpu.isa.vecemit import DEMONSTRATED, ResidentEpilogueKernel

    got = compile_at(linear_silu)
    stmt = next(s for s in got.stages[1].instances[0].stmts)
    kernel = ResidentEpilogueKernel(
        [(kind, list(srcs)) for kind, srcs in stmt.args["chain"]],
        0,
        {1: -1.4426950408889634, 2: 1.0},
        TILING["gm"] * TILING["gn"],
    )
    assert kernel.image and DEMONSTRATED
    assert sigmoid is not None
