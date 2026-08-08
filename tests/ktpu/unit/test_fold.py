"""Epilogue folding: that it happens, and that it lands on the producer's tiles.

Every test here exists because the suite was green while folding was broken. A
fold that silently drops the epilogue and a fold whose grid names none of the
producer's tiles both produce a schedule that lowers, codegens and prints.
"""

import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.codegen.cu import vector_spans
from ktpu.ir import FP16, OpKind
from ktpu.ir.program import Opcode
from ktpu.ir.sched import (
    Band,
    Engine,
    Grid,
    SchedOp,
    Schedule,
    ScheduleError,
    Tile,
    correspondence,
)
from ktpu.passes import lower
from ktpu.passes.tile import epilogue_grid
from ktpu.target import VU13P_8CU, Target

M, K, N = 256, 1024, 256


def _graph():
    return D.trace(
        lambda x, w, b: D.gelu(x @ w + b),
        D.TensorSpec((M, K), FP16),
        D.TensorSpec((K, N), FP16),
        D.TensorSpec((N,), FP16),
    )


def _bands(fold=True):
    return lower(_graph(), VU13P_8CU, fold_epilogue=fold).bands


# ---------------------------------------------------------------------------
# It happens at all
# ---------------------------------------------------------------------------


def test_the_epilogue_folds():
    mm, ep = _bands()
    assert ep.consumes == mm.name


def test_no_second_vector_band_reads_the_result_from_dram():
    """The failure mode is an EXTRA band, not a missing flag: a fold that half
    applied would leave both."""
    bands = _bands()
    assert [b for b in bands if b.engine is Engine.VECTOR and b.consumes is None] == []


def test_folding_does_not_drop_the_work():
    """The regression that shipped once: the ops went into the matmul band and
    the matmul emitter never looked at them, so gelu simply vanished and the
    instruction count fell by 10x."""
    folded = [o.kind for b in _bands(True) for o in b.ops]
    flat = [o.kind for b in _bands(False) for o in b.ops]
    assert sorted(folded, key=str) == sorted(flat, key=str)
    assert folded.count(OpKind.EXP2) == 1


def test_disabling_the_fold_gives_a_dram_reading_band():
    mm, ep = _bands(fold=False)
    assert ep.consumes is None
    assert mm.engine is Engine.MATMUL


# ---------------------------------------------------------------------------
# The grid corresponds -- the fault the element totals hid
# ---------------------------------------------------------------------------


def test_the_folded_grid_walks_the_producers_tiles():
    mm, ep = _bands()
    assert correspondence(mm, ep) == (1, 1)
    assert (ep.tile.m, ep.tile.n) == (mm.tile.m, mm.tile.n)
    assert ep.grid.size == mm.grid.size


def test_the_flat_epilogue_that_shipped_is_rejected():
    """The exact shape the broken schedule had: 64 instances of 1024x1 against
    8 of 64x128. Both are 65,536 elements, which is what made it look fine."""
    s = Schedule()
    mm = s.add(
        Band(
            Engine.MATMUL,
            Grid(m=4, n=2),
            Tile(m=64, n=128, k=128),
            ops=[SchedOp(OpKind.MATMUL, {"m": M, "k": K, "n": N})],
        )
    )
    s.add(Band(Engine.VECTOR, Grid(m=64), Tile(m=1024), consumes=mm.name))
    assert 64 * 1024 == 8 * 64 * 128
    with pytest.raises(ScheduleError, match="does not refine"):
        s.verify()


def test_equal_element_totals_are_not_a_correspondence():
    """A grid that DOES refine the producer's and still walks the wrong tiles:
    16 instances of 64x64 is 65,536 elements over a 4x2 grid of 64x128, so the
    totals agree axis by axis and instance j still names no tile."""
    s = _pair(Grid(m=8, n=2), Tile(m=64, n=64))
    assert 16 * 64 * 64 == 8 * 64 * 128
    with pytest.raises(ScheduleError, match="names no producer tile"):
        s.verify()


def _pair(grid: Grid, tile: Tile, **kw) -> Schedule:
    s = Schedule()
    mm = s.add(
        Band(
            Engine.MATMUL,
            Grid(m=4, n=2, sk=kw.pop("sk", 1)),
            Tile(m=64, n=128, k=128),
            ops=[SchedOp(OpKind.MATMUL, {"m": M, "k": K, "n": N})],
        )
    )
    s.add(Band(kw.pop("engine", Engine.VECTOR), grid, tile, consumes=mm.name, **kw))
    return s


def test_a_grid_that_does_not_divide_the_producers_is_rejected():
    with pytest.raises(ScheduleError, match="does not refine"):
        _pair(Grid(m=3, n=2), Tile(m=64, n=128)).verify()


def test_a_refined_grid_needs_a_correspondingly_smaller_tile():
    with pytest.raises(ScheduleError, match="is not .*tile"):
        _pair(Grid(m=8, n=2), Tile(m=64, n=128)).verify()


def test_consuming_partials_must_be_a_reduction_instead():
    """A split-K producer holds PARTIALS, so the band behind it sums S of them;
    reading one as if it were the answer is a silently short result. The
    reduction band here satisfies the split-K rule, so what fires is the
    consumes rule and nothing else."""
    s = _pair(Grid(m=4, n=2), Tile(m=64, n=128), sk=4)
    s.add(
        Band(
            Engine.VECTOR,
            Grid(m=8),
            Tile(m=8192),
            ops=[SchedOp(OpKind.ADD)],
            reduces="b0",
        )
    )
    with pytest.raises(ScheduleError, match="reduces= them rather than consumes="):
        s.verify()


def test_consuming_a_band_that_runs_later_is_rejected():
    s = Schedule()
    s.add(Band(Engine.VECTOR, Grid(m=4, n=2), Tile(m=64, n=128), consumes="mm"))
    s.add(
        Band(
            Engine.MATMUL,
            Grid(m=4, n=2),
            Tile(m=64, n=128, k=128),
            ops=[SchedOp(OpKind.MATMUL, {"m": M, "k": K, "n": N})],
            name="mm",
        )
    )
    with pytest.raises(ScheduleError, match="runs after it"):
        s.verify()


def test_only_a_matmul_band_holds_something_to_consume():
    s = Schedule()
    s.add(Band(Engine.VECTOR, Grid(m=1), Tile(m=8), name="v0"))
    s.add(Band(Engine.VECTOR, Grid(m=1), Tile(m=8), consumes="v0"))
    with pytest.raises(ScheduleError, match="only a cluster holds a tile"):
        s.verify()


def test_consuming_an_unknown_band_is_rejected():
    s = Schedule()
    s.add(Band(Engine.VECTOR, Grid(m=1), Tile(m=8), consumes="nope"))
    with pytest.raises(ScheduleError, match="consumes unknown band"):
        s.verify()


# ---------------------------------------------------------------------------
# Splitting a producer tile across more vector cores than there are clusters
# ---------------------------------------------------------------------------


def _producer(gm: int, gn: int, tm: int, tn: int) -> Band:
    return Band(
        Engine.MATMUL,
        Grid(m=gm, n=gn),
        Tile(m=tm, n=tn, k=128),
        ops=[SchedOp(OpKind.MATMUL, {"m": gm * tm, "k": K, "n": gn * tn})],
        name="mm",
    )


def test_a_narrow_matmul_grid_still_fills_the_vector_mesh():
    """Two output tiles and eight vector cores: each tile splits four ways by
    rows, and the result is still a correspondence."""
    p = _producer(2, 1, 64, 128)
    grid, tile = epilogue_grid(p, VU13P_8CU)
    assert grid.size == 8
    assert (tile.m, tile.n) == (16, 128)
    assert correspondence(p, Band(Engine.VECTOR, grid, tile, name="ep")) == (4, 1)


def test_a_matching_grid_is_left_alone():
    p = _producer(4, 2, 64, 128)
    grid, tile = epilogue_grid(p, VU13P_8CU)
    assert (grid.m, grid.n) == (4, 2)
    assert (tile.m, tile.n) == (64, 128)


def test_tiles_are_not_split_below_one_pass_of_the_machine():
    """A slice under VLMAX costs more in per-instance setup than the extra core
    returns, so the split stops even with cores to spare."""
    p = _producer(1, 1, 8, 4)
    grid, tile = epilogue_grid(p, Target(vector_cores=64))
    assert grid.size == 1
    assert (tile.m, tile.n) == (8, 4)


def test_a_split_never_cuts_a_sub_tile_in_half():
    p = _producer(1, 1, 64, 128)
    t = Target(vector_cores=64)
    _, tile = epilogue_grid(p, t)
    assert tile.m % t.lanes == 0


# ---------------------------------------------------------------------------
# Level 3: what the fold actually changes in the instruction stream
# ---------------------------------------------------------------------------


def _prog(fold=True):
    return codegen(lower(_graph(), VU13P_8CU, fold_epilogue=fold), M, K, N, VU13P_8CU)


def _of(prog, op):
    return [i for i in prog.insts if i.op is op]


def _in(region, inst):
    return region.word <= inst.fields["word"] < region.end


def test_a_16_op_chain_touches_memory_exactly_twice():
    """The one that matters for overhead. VMODE bounds how many ops CHAIN --
    bypass the register file between adjacent ALUs -- it does not bound how long
    a value lives. Sixteen ops is one load and one store per chunk, plus the
    bias broadcast; treating each 4-op group as a checkpoint to DRAM cost a
    245,760-word scratch region and 40% more vector instructions for the same
    arithmetic."""
    prog = _prog()
    body = [i for i in prog.insts if i.node == (1, 1)]
    lds = [i for i in body if i.op is Opcode.VLD]
    sts = [i for i in body if i.op is Opcode.VST]
    assert len([i for i in body if i.op is Opcode.VALU]) == 16
    assert len(sts) == 1
    assert len(lds) == 2
    assert sum(1 for i in lds if i.fields.get("bcast", "elem") != "elem") == 1


def test_a_chain_leaves_no_scratch_region_at_all():
    prog = _prog()
    assert [r.name for r in prog.memory.regions if r.name == "tmp"] == []


def test_no_fp16_appears_anywhere_in_the_middle_of_the_folded_path():
    """The whole point of the fold. Any FP16 before the last store would be a
    rounding AND a clamp at 65504 applied to a value that is not final yet.

    The bias is a broadcast of a graph input, genuinely FP16 in DRAM and not the
    intermediate, so it is excluded by its `bcast` axis rather than by dtype."""
    prog = _prog()
    acc = prog.memory.get("acc")
    lds = [i for i in _of(prog, Opcode.VLD) if i.fields.get("bcast", "elem") == "elem"]
    assert lds
    for i in lds:
        assert i.fields["dtype"] == "acc24" and _in(acc, i)
    for i in _of(prog, Opcode.DRAIN):
        assert i.fields["dtype"] == "acc24" and _in(acc, i)


def test_the_bias_is_a_broadcast_load_and_not_a_materialised_tensor():
    """`x @ w + b` reads b as one value per COLUMN, from a region holding 256
    numbers. Expanding it to the full 256x256 operand would be 65,536 elements
    of DRAM for the same 256."""
    prog = _prog()
    bias = [i for i in _of(prog, Opcode.VLD) if i.fields.get("bcast") == "col"]
    assert bias and {i.fields["src"] for i in bias} == {"b"}
    r = prog.memory.get("v2")
    assert r.elems == N and all(_in(r, i) for i in bias)


def test_exactly_one_store_per_core_and_it_is_the_only_conversion():
    prog = _prog()
    c = prog.memory.get("c")
    sts = _of(prog, Opcode.VST)
    assert len({i.node for i in sts}) == len(sts) == VU13P_8CU.vector_cores
    assert all(i.fields["dtype"] == "fp16" and _in(c, i) for i in sts)


def test_without_the_fold_the_matmul_output_is_fp16_and_read_back_as_fp16():
    """No `acc` region at all: the clusters convert on the way out, and the
    vector band reads that converted result from the matmul's OWN region --
    which is not `c`, because `c` is the final output and these are two
    different buffers once every band has one."""
    prog = _prog(fold=False)
    with pytest.raises(KeyError):
        prog.memory.get("acc")
    mm_out = prog.memory.get("v3")
    first = [
        i
        for i in _of(prog, Opcode.VLD)
        if i.fields["dtype"] == "fp16" and i.fields.get("bcast", "elem") == "elem"
    ]
    assert first and all(_in(mm_out, i) for i in first)
    assert all(_in(mm_out, i) for i in _of(prog, Opcode.DRAIN))
    assert prog.memory.get("c").name != mm_out.name


def test_the_load_that_reads_a_resident_tile_names_the_producer_band():
    prog = _prog()
    named = [i for i in _of(prog, Opcode.VLD) if "tile" in i.fields]
    assert named and {i.fields["src"] for i in named} == {"b0"}
    assert all(i.fields["dtype"] == "acc24" for i in named)


# ---------------------------------------------------------------------------
# Addresses: the units bug that put all eight tiles on top of each other
# ---------------------------------------------------------------------------


def test_the_fills_tile_each_operand_exactly_once():
    """`n` is in L1 ENTRIES and the address is in WORDS. Advancing the address
    by the entry count put all 64 A-fills in the first 1,984 words of a 131,072
    word region -- every cluster reading the same corner of A."""
    prog = _prog()
    for sel in ("a", "b"):
        r = prog.memory.get(sel)
        starts = sorted(
            {i.fields["word"] for i in _of(prog, Opcode.FILL) if i.fields["sel"] == sel}
        )
        stride = starts[1] - starts[0]
        assert starts == [r.word + j * stride for j in range(len(starts))]
        assert starts[-1] + stride == r.end


def test_the_drains_tile_the_output_exactly_once():
    """`n` is in SUB-TILES and the address is in WORDS (isa/cluster.md s5). Held
    in one unit, the eight tiles overlapped 8:1 and the last writer won."""
    prog = _prog()
    acc = prog.memory.get("acc")
    drains = _of(prog, Opcode.DRAIN)
    tile_words = (acc.end - acc.word) // len(drains)
    starts = sorted(i.fields["word"] for i in drains)
    assert starts == [acc.word + j * tile_words for j in range(len(drains))]


def test_each_vector_core_stores_the_tile_its_cluster_drained():
    """The correspondence, checked at level 3 rather than asserted at level 2:
    C spans and drain spans are the same set in the same order."""
    prog = _prog()
    acc, c = prog.memory.get("acc"), prog.memory.get("c")
    drained = [i.fields["word"] - acc.word for i in _of(prog, Opcode.DRAIN)]
    stored = sorted(
        {
            i.fields["word"] - c.word
            for i in _of(prog, Opcode.VST)
            if i.fields["dtype"] == "fp16"
        }
    )
    assert len(stored) == len(drained)
    assert stored == sorted(stored)
    assert [d // (drained[1] - drained[0]) for d in sorted(drained)] == [
        s // (stored[1] - stored[0]) for s in stored
    ]


def test_one_run_per_core_when_the_grid_matches_the_mesh():
    """Instances are dealt in contiguous blocks, so a core's work is ONE address
    descriptor. Round-robin dealing would give eight strided runs instead."""
    prog = _prog()
    per_core: dict[tuple[int, int], int] = {}
    for i in _of(prog, Opcode.VSETVL):
        per_core[i.node] = per_core.get(i.node, 0) + 1
    assert len(per_core) == VU13P_8CU.vector_cores
    loops = {}
    for i in _of(prog, Opcode.VLOOP):
        loops.setdefault(i.node, set()).add(i.fields["count"])
    assert all(v == {64} for v in loops.values())


def test_vector_spans_cover_the_output_exactly_once():
    mm, ep = _bands()
    spans = vector_spans(ep, mm)
    elems = ep.tile.m * ep.tile.n
    assert sorted(s[0] for s in spans) == [j * elems for j in range(len(spans))]
    assert len(spans) * elems == M * N
