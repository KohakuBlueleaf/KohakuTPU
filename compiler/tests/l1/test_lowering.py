"""L1 -> L0: what a Program becomes as a package, step by step and word by word."""

import pytest
from kohakuaccel.package.format import Op
from kohakutpu.hw import vector as V
from kohakutpu.hw import vector2 as V2
from kohakutpu.hw.vector2 import Kernel2
from kohakutpu.imem import Resident
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ir.l1.vector import Desc, Dims, Halt, Image, Run, Seti
from kohakutpu.ir.l1.vector2 import Kernel
from kohakutpu.isa.vector import ISA as VEC

from kohakutpu import imem

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


def test_a_v2_image_past_512_words_carries_bit_9_at_flit_bit_242():
    payload = (1 << 256) - 1
    for a in (0, 1, 511, 512, 777, 1023):
        assert VEC.imem(a, 0xABC) == V2.imem_flit(a, 0xABC) & payload
        assert VEC.imem_addr(VEC.IMEM.decode(VEC.imem(a, 0))) == a


def _kern(n: int, tag: int):
    k = Kernel2()
    for i in range(n - 1):
        k.emit(tag << 20 | i)
    k.emit(V2.vhalt())
    return k


def _imem_addrs(words):
    out = []
    for w in words:
        if imem.op(w) == 1:
            out.append(VEC.imem_addr(VEC.IMEM.decode(w & imem.PAYLOAD)))
    return out


def test_v2_programs_share_1024_words_and_start_below_the_run_reach():
    p = Program(MACHINE)
    vc = p.units("VC")[0]
    cores = imem.Cores(imem.IMEM_WORDS_V2)
    a, b, big = _kern(600, 1), _kern(300, 2), _kern(900, 3)
    sent = []
    for k in (a, b, a, big, a):
        sent.append(imem.rewrite(Kernel(k).flits(), cores[vc]))
    assert _imem_addrs(sent[0]) == list(range(600))
    assert max(_imem_addrs(sent[0])) > 511
    # b fits past a, at 600 -- but a RUN cannot start there; a is evicted.
    assert _imem_addrs(sent[1])[0] == 0
    assert _imem_addrs(sent[2])[0] == 300, "a again: below 512 once b holds 0..299"
    assert _imem_addrs(sent[3])[0] == 0
    runs = [VEC.RUN.decode(w & imem.PAYLOAD)["pc"] for w in sent[4] if imem.op(w) == 3]
    assert runs == [0] and len(_imem_addrs(sent[4])) == 600


def test_a_variant_over_a_resident_program_sends_only_the_words_that_differ():
    p = Program(MACHINE)
    vc = p.units("VC")[0]
    cores = imem.Cores(imem.IMEM_WORDS_V2)
    a, b = _kern(600, 1), _kern(600, 1)
    b.words[7] = 5 << 20 | 7
    b.words[300] = 5 << 20 | 300
    sent = [imem.rewrite(Kernel(k).flits(), cores[vc]) for k in (a, b, a, a)]
    assert _imem_addrs(sent[0]) == list(range(600))
    assert _imem_addrs(sent[1]) == [7, 300], "b replaces a at 0: two words differ"
    assert _imem_addrs(sent[2]) == [7, 300] and _imem_addrs(sent[3]) == []
    cores[vc].forget()
    assert _imem_addrs(imem.rewrite(Kernel(a).flits(), cores[vc])) == list(range(600))


def test_a_fetch_step_counts_its_words_as_sent_to_the_unit():
    p = Program(MACHINE)
    u = p.units("MG")[0]
    p.send(u, Fill(0x1000, 4)).fetch(u, 0x80_0000, 3).wait(u)
    got = p.build((3, 1), None).build(defaults=False).steps
    ops = [(s.op, s.count) for s in got if s.op != Op.BARRIER]
    assert ops == [(Op.DISPATCH, 1), (Op.FETCH, 3), (Op.AWAIT, 4)]
    assert next(s.arg for s in got if s.op == Op.FETCH) == 0x80_0000


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
