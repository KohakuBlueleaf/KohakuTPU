"""L1 -> L0: what a Program becomes as a package, step by step and word by word."""

import pytest
from kohakuaccel.package.format import Op
from kohakutpu.hw import vector as V
from kohakutpu.imem import Resident
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ir.l1.vector import Desc, Dims, Halt, Image, Run, Seti

MACHINE = L1Model().machine


def steps(prog, resident=None):
    return prog.build(None, resident).build(defaults=False).steps


def test_sends_between_two_syncs_coalesce_into_one_dispatch_per_unit():
    """A dispatch per unit per sync interval, interleaved sends or not: a fetch
    request costs the same whatever its length."""
    p = Program(MACHINE)
    a, b = p.units("MG")[:2]
    p.send(a, Fill(0x1000, 4)).send(b, Fill(0x2000, 4)).send(a, Gemm(1, 1, 2))
    p.barrier()
    got = steps(p)
    sends = [s for s in got if s.op == Op.DISPATCH]
    assert [s.count for s in sends] == [2, 1]
    assert [s.count for s in got if s.op == Op.AWAIT] == [2, 1]


def test_a_wait_awaits_exactly_the_words_sent_to_that_unit_since_its_last_wait():
    p = Program(MACHINE)
    u = p.units("MG")[0]
    p.send(u, Fill(0, 1), Fill(0, 1)).wait(u).send(u, Gemm(1, 1, 2)).wait(u)
    got = [(s.op, s.count) for s in steps(p) if s.op in (Op.DISPATCH, Op.AWAIT)]
    assert got == [(Op.DISPATCH, 2), (Op.AWAIT, 2), (Op.DISPATCH, 1), (Op.AWAIT, 1)]


def test_a_resident_image_and_an_unchanged_descriptor_are_not_sent_again():
    p = Program(MACHINE)
    vc = p.units("VC")[0]
    img = Image((Seti(0, 128), Halt()))
    p.send(vc, img, Desc(1, 0x1000), Dims(1, ((32, 4),)), Run(0))
    resident = {}
    first = sum(s.count for s in steps(p, resident) if s.op == Op.DISPATCH)
    again = sum(s.count for s in steps(p, resident) if s.op == Op.DISPATCH)
    assert first == 3 + 1 + 4 + 1
    assert again == 1, "only the RUN: image and descriptors are already in the core"
    assert isinstance(resident[vc], Resident)


def test_an_emitting_gemm_that_would_open_its_tile_is_refused_at_l1():
    """No load-and-emit accumulator op: the fused DRAIN would wait forever."""
    with pytest.raises(ValueError, match="opens its tile"):
        Gemm(8, 8, 2, emit=True).flits()
    assert (
        Gemm(8, 8, 4, emit=True).flits() and Gemm(8, 8, 2, acc=True, emit=True).flits()
    )


@pytest.mark.parametrize("nk", [3, 5, 7])
def test_an_odd_nk_past_one_is_refused_at_l1(nk):
    """Card-measured: sub-tile (0, 0) wrong at nk 3 and 5; nk 1 and even exact."""
    with pytest.raises(ValueError, match="odd nk"):
        Gemm(8, 8, nk).flits()
    assert Gemm(8, 8, 1).flits() and Gemm(8, 8, nk + 1).flits()


def test_encodings_that_would_alias_are_refused_not_truncated():
    with pytest.raises(ValueError):
        Fill(0, 256).flits()
    with pytest.raises(V.VectorEncodeError):
        Image((Seti(16, 0),)).flits()
    with pytest.raises(ValueError, match="four"):
        Dims(1, ((1, 1),) * 5).flits()
    with pytest.raises(ValueError, match="IMEM"):
        Image((Halt(),) * 513).flits()


def test_a_fused_drain_is_one_word_with_its_fuse_bit():
    (w,) = Drain(0x4000, 64, fuse=True).flits()
    assert (w >> 115) & 1
