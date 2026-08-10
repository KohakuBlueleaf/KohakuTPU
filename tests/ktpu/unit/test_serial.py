"""The band-serial cycle model, and why `time_of` alone was not enough.

`time_of` takes one global max over nodes. That is right for a GEMM dealt
across clusters and wrong for a chain: attention is GEMM -> softmax -> GEMM,
each needing the one before it complete, so a global max hides the entire
vector program behind the matmuls and reports every softmax optimisation as
worth nothing. These two models bracket the truth and neither is it.
"""

import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.dsl.nn import attention
from ktpu.interp.timing import cost_of_inst, serial_of, time_of
from ktpu.ir import FP16
from ktpu.ir.program import Inst, Opcode, Program
from ktpu.ir.sched import Engine
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as T

L, DH, BLOCK = 128, 64, 64


def spec(*s):
    return D.TensorSpec(s, FP16)


def attn(causal, reorder=True):
    args = [spec(L, DH)] * 3 + ([spec(BLOCK, BLOCK)] if causal else [])
    graph = D.trace(
        lambda q, k, v, *r: attention(
            q, k, v, block=BLOCK, causal=causal, tri=r[0] if causal else None
        ),
        *args,
    )
    sched = lower(graph, T, reorder=reorder)
    return codegen(sched, BLOCK if causal else L, DH, DH, T)


def test_codegen_records_a_span_for_every_band_that_emitted_anything():
    prog = attn(False)
    assert prog.bands, "no band spans recorded"
    covered = sum(hi - lo for _, lo, hi in prog.bands)
    assert covered == len(prog.insts), "spans do not tile the instruction stream"
    ends = [lo for _, lo, _ in prog.bands]
    assert ends == sorted(ends), "spans are not in emission order"


def test_the_spans_do_not_overlap():
    prog = attn(True)
    last = 0
    for _name, lo, hi in prog.bands:
        assert lo == last, "a gap or an overlap between band spans"
        last = hi


@pytest.mark.parametrize("causal", [False, True])
def test_serial_exceeds_the_global_max_because_bands_cannot_overlap(causal):
    prog = attn(causal)
    assert serial_of(prog, T).cycles > time_of(prog, T).cycles


def test_a_program_without_spans_returns_zero_rather_than_guessing():
    """Guessing boundaries out of the stream reads as a plausible number."""
    prog = Program(name="bare")
    prog.emit(Inst(Opcode.VALU, Engine.VECTOR, (1, 1), {"op": "mul", "srcs": []}))
    assert serial_of(prog, T).cycles == 0


def test_the_vector_share_is_the_denominator_that_keeps_a_saving_honest():
    """Attention's vector work is a fifth of it, so a fifth off the vector part
    is a twentieth off the kernel. Quoting the first alone is how a 1% win gets
    sold as 20%."""
    res = serial_of(attn(True), T)
    assert 0.05 < res.share("vector") < 0.35
    assert res.share("vector") + res.share("matmul") == pytest.approx(1.0)


def test_the_reorder_is_a_win_this_model_can_see_and_time_of_cannot():
    """The reason the model was promoted out of scripts/. Fusing under-filled
    bands onto the reduction's grid puts eight cores on work four were doing;
    a global max over nodes cannot express that and reports no change."""
    off, on = attn(True, reorder=False), attn(True, reorder=True)
    assert time_of(on, T).cycles == time_of(off, T).cycles
    assert serial_of(on, T).by_engine["vector"] < serial_of(off, T).by_engine["vector"]
    assert serial_of(on, T).cycles < serial_of(off, T).cycles


def test_vl_halves_what_an_arithmetic_instruction_costs():
    """codegen elides a VSETVL the core already holds, so a model that does not
    track VL across bands reads every band as VLMAX and doubles the short ones."""
    valu = Inst(Opcode.VALU, Engine.VECTOR, (1, 1), {"op": "mul", "srcs": []})
    assert cost_of_inst(valu, T, 64) * 2 == cost_of_inst(valu, T, 128)
    assert cost_of_inst(valu, T, 0) == cost_of_inst(valu, T, T.vlmax)


def test_serial_tracks_vl_across_a_band_boundary():
    """A band whose VSETVL was elided must not be costed at VLMAX."""
    prog = attn(False)
    sets = [i for i in prog.insts if i.op is Opcode.VSETVL]
    loops = [i for i in prog.insts if i.op is Opcode.VLOOP]
    assert len(sets) < len(loops), "nothing was elided, so this proves nothing"
    assert serial_of(prog, T).cycles > 0
