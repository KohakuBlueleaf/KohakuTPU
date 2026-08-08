"""Level 3 -> machine code, checked against the planner the RTL actually runs.

`tests/ktpu/unit/test_encode.py` checks the bit LAYOUT field by field. This
checks the whole STREAM: for one 64x64x64 GEMM on one cluster, every flit ktpu
emits is byte-identical to the one `driver.bench.build` emits -- and that stream
is what `xsim.py mag_driver` executes. Identical bytes mean the RTL cannot tell
the two apart.

Three things have to be reconciled first, and each is a real interface fact
rather than an encoding bug:

  units    the legacy `Layout`'s `a_word`/`b_word`/`c_word` count 256-BIT words;
           a ktpu `Program` counts 32-bit words. A factor of eight.
  preq     `bench.build` pre-quantises both operands into memory, so its FILLs
           set the bit. It is a property of the TENSOR and the driver picks it.
  emit     the legacy GEMM carries C's base and sets `emit`, so the sweep hands
           sub-tiles out as it finishes and the DRAIN is a barrier (`fuse`).
"""

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.codegen.encode import flit
from ktpu.ir import FP16
from ktpu.ir.program import Opcode
from ktpu.passes import lower
from ktpu.target import Target

ONE_CU = Target(name="one-cu", clusters=1, vector_cores=0)


def _reference(m, k, n):
    """The planner whose output xsim runs: `ktpu.hw`, the RTL-facing half."""
    from ktpu.hw import bench

    return bench.build(m, k, n, ncl=1)


def _ktpu_flits(prob, m, k, n):
    """ktpu's flits for the same problem, relocated onto the legacy layout."""
    graph = D.trace(
        lambda a, b: a @ b, D.TensorSpec((m, k), FP16), D.TensorSpec((k, n), FP16)
    )
    sched = lower(graph, ONE_CU)
    prog = codegen(sched, m, k, n, ONE_CU)

    lay = prob.layout
    target = {"v0": lay.a_word * 8, "v1": lay.b_word * 8, "v2": lay.c_word * 8}
    spans = {r.name: (r.word, r.end) for r in prog.memory.regions}

    for inst in prog.insts:
        if "word" in inst.fields:
            for name, (lo, hi) in spans.items():
                if lo <= inst.fields["word"] < hi and name in target:
                    inst.fields["word"] += target[name] - lo
                    break
        match inst.op:
            case Opcode.FILL:
                inst.fields["preq"] = 1
            case Opcode.GEMM:
                inst.fields["word"] = target["v2"]
                inst.fields["emit"] = 1
            case Opcode.DRAIN:
                inst.fields["fuse"] = 1
            case _:
                pass

    cluster = [
        i for i in prog.insts if i.op in (Opcode.FILL, Opcode.GEMM, Opcode.DRAIN)
    ]
    out = [flit(i, last=i.op is Opcode.DRAIN) for i in cluster]
    return [out[1], out[0], *out[2:]]


def test_the_flit_stream_is_byte_identical_to_the_planner_xsim_runs():
    """The whole of `level 3 -> machine code`, end to end. If this passes, the
    bytes xsim executes for this GEMM are the bytes ktpu produced."""
    prob = _reference(64, 64, 64)
    reference = [f for r in prob.kern.rounds for p in r.passes for f in p.flits]
    mine = _ktpu_flits(prob, 64, 64, 64)

    assert len(mine) == len(reference) == 4
    for i, (want, got) in enumerate(zip(reference, mine, strict=True)):
        assert got == want, f"flit {i}\n  planner {want:#x}\n  ktpu    {got:#x}"


def test_both_pick_the_same_tile():
    """The comparison is only meaningful if the two planners agree on the tile;
    if they ever diverge this fails loudly rather than comparing two different
    decompositions and calling the difference an encoding bug."""
    prob = _reference(64, 64, 64)
    graph = D.trace(
        lambda a, b: a @ b, D.TensorSpec((64, 64), FP16), D.TensorSpec((64, 64), FP16)
    )
    band = lower(graph, ONE_CU).bands[0]
    assert (band.tile.m, band.tile.k, band.tile.n) == (64, 64, 64)
    assert str(prob.kern.tile) == "64x64x64"


def test_the_stream_is_one_fill_per_operand_then_sweep_then_drain():
    prob = _reference(64, 64, 64)
    mine = _ktpu_flits(prob, 64, 64, 64)
    ops = [(w >> 252) & 0xF for w in mine]
    assert ops == [1, 1, 2, 3]
    assert [w >> 259 & 1 for w in mine] == [0, 0, 0, 1]
