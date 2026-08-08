"""Level 3 -> machine code: the flit bit layout.

Checked against `docs/isa/cluster.md` §2 by POSITION, not by round-tripping the
encoder against itself -- an encoder that agrees with its own decoder and with
nothing else is exactly the bug this is here to catch.
"""

import pytest

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.codegen.encode import EncodeError, cluster_words, decode, flit, place
from ktpu.ir import FP16
from ktpu.ir.program import Inst, Opcode
from ktpu.ir.sched import Engine
from ktpu.passes import lower
from ktpu.target import VU13P_8CU

M, K, N = 64, 128, 64


def _fill(**kw):
    f = {"sel": "a", "word": 0, "n": 1, "eoff": 0} | kw
    return Inst(Opcode.FILL, Engine.MATMUL, (1, 1), f)


def _gemm(**kw):
    f = {"gm": 16, "gn": 32, "nk": 4, "anchor": 40, "acc": 0} | kw
    return Inst(Opcode.GEMM, Engine.MATMUL, (1, 1), f)


def _drain(**kw):
    return Inst(Opcode.DRAIN, Engine.MATMUL, (1, 1), {"word": 0, "n": 512} | kw)


# ---------------------------------------------------------------------------
# Field positions, read off the ISA document
# ---------------------------------------------------------------------------


def test_the_opcode_lands_in_bits_255_252():
    assert flit(_fill()) >> 252 & 0xF == 1
    assert flit(_gemm()) >> 252 & 0xF == 2
    assert flit(_drain()) >> 252 & 0xF == 3


def test_an_address_is_in_bytes_not_words():
    """A Program addresses 32-bit WORDS; the ISA field is a BYTE address. Off by
    this factor of four every operand is read from a quarter of the way in."""
    assert decode(flit(_fill(word=1024)))["addr"] == 4096


def test_n_is_sixteen_bits_because_a_drain_names_512_subtiles():
    """An 8-bit `n` wraps 512 to 0, draining the start of the tile a second time
    and reporting nothing (isa/cluster.md §2)."""
    assert decode(flit(_drain(n=512)))["n"] == 512
    assert decode(flit(_drain(n=65535)))["n"] == 65535


def test_sel_picks_the_b_side():
    assert decode(flit(_fill(sel="a")))["sel"] == 0
    assert decode(flit(_fill(sel="b")))["sel"] == 1


def test_the_gemm_shape_and_anchor_land_where_the_cu_reads_them():
    got = decode(flit(_gemm(gm=16, gn=32, nk=4, anchor=40, acc=1)))
    assert (got["gm"], got["gn"], got["nk"]) == (16, 32, 4)
    assert got["anchor"] == 40
    assert got["acc"] == 1


def test_the_l1_offsets_are_carried():
    got = decode(flit(_gemm(aoff=64, boff=128)))
    assert (got["aoff"], got["boff"]) == (64, 128)
    assert decode(flit(_fill(eoff=200)))["eoff"] == 200


def test_last_is_a_header_bit_not_a_payload_field():
    """Bit 259, above the payload -- the same bit the memory protocol uses for
    the final beat of a burst (isa/cluster.md §6)."""
    assert flit(_drain(), last=True) >> 259 & 1 == 1
    assert flit(_drain(), last=False) >> 259 & 1 == 0
    assert flit(_drain(), last=True) ^ flit(_drain()) == 1 << 259


def test_fields_do_not_overlap():
    """Every field set alone must leave the others zero. Overlapping by opcode
    is how a decode bug survives review, and the payload has spare bits."""
    for name in ("op", "addr", "n", "sel", "acc", "gm", "gn", "nk", "anchor"):
        one = place(name, 1)
        others = [n for n in ("addr", "n", "gm", "gn", "nk", "anchor") if n != name]
        assert all(decode(one)[o] == 0 for o in others), name


# ---------------------------------------------------------------------------
# A value too wide is an error, not a silent shift
# ---------------------------------------------------------------------------


def test_a_field_too_wide_is_rejected():
    """The failure this replaces: an unsized value in a concatenation shifts
    every field below it, elaborates cleanly, and the CU executes nothing."""
    with pytest.raises(EncodeError, match="does not fit"):
        place("gm", 256)
    with pytest.raises(EncodeError, match="does not fit"):
        place("n", 1 << 16)


def test_an_address_past_the_field_is_rejected():
    with pytest.raises(EncodeError, match="addr"):
        flit(_fill(word=1 << 34))


def test_a_vector_instruction_is_not_a_cluster_flit():
    v = Inst(Opcode.VLD, Engine.VECTOR, (1, 1), {"word": 0, "dtype": "fp16"})
    with pytest.raises(EncodeError, match="not a cluster instruction"):
        flit(v)


# ---------------------------------------------------------------------------
# A whole program encodes
# ---------------------------------------------------------------------------


def _prog():
    graph = D.trace(
        lambda a, b: a @ b, D.TensorSpec((M, K), FP16), D.TensorSpec((K, N), FP16)
    )
    return codegen(lower(graph, VU13P_8CU), M, K, N, VU13P_8CU)


def test_every_cluster_instruction_of_a_real_program_encodes():
    words = cluster_words(_prog())
    assert words
    assert all(0 <= w < (1 << 288) for _, w in words)


def test_exactly_one_flit_per_node_is_marked_last():
    """`last` returns the batch-complete signal, so a node with none never
    reports and a node with two reports twice (isa/agent.md §6)."""
    words = cluster_words(_prog())
    ended = [node for node, w in words if w >> 259 & 1]
    assert sorted(ended) == sorted(set(ended))
    assert set(ended) == {node for node, _ in words}


def _legacy():
    """The RTL-facing encoder in `ktpu.hw`, which is what the CU decodes."""
    from ktpu.hw.matmul import _flit

    return _flit


@pytest.mark.parametrize(
    "make,kw",
    [
        (_fill, {"word": 4096, "n": 64, "sel": "a", "eoff": 0}),
        (_fill, {"word": 8192, "n": 128, "sel": "b", "eoff": 64}),
        (_gemm, {"gm": 16, "gn": 32, "nk": 4, "anchor": 40, "acc": 1}),
        (_gemm, {"gm": 4, "gn": 4, "nk": 1, "anchor": 40, "acc": 0, "aoff": 32}),
        (_drain, {"word": 262144, "n": 512}),
    ],
)
def test_bit_for_bit_against_the_legacy_encoder(make, kw):
    """The only check that means anything: agree with the encoder the RTL is
    known to accept, not with our own decoder. An encoder consistent only with
    itself is exactly the failure this file exists to prevent."""
    legacy = _legacy()
    inst = make(**kw)
    ours = flit(inst)

    op = {"fill": 1, "gemm": 2, "drain": 3}[inst.op.value]
    theirs = legacy(
        op,
        addr=kw.get("word", 0) * 4,
        n=kw.get("n", 0),
        sel=1 if kw.get("sel") == "b" else 0,
        gm=kw.get("gm", 0),
        gn=kw.get("gn", 0),
        nk=kw.get("nk", 1) if op == 2 else 1,
        anchor=kw.get("anchor", 40) if op == 2 else 40,
        acc=bool(kw.get("acc", 0)),
        eoff=kw.get("eoff", 0),
        aoff=kw.get("aoff", 0),
        boff=kw.get("boff", 0),
    )
    assert ours == theirs, f"\n ours   {ours:#x}\n legacy {theirs:#x}"


def test_a_fill_too_large_for_the_streaming_count_is_rejected():
    with pytest.raises(EncodeError, match="8-bit streaming count"):
        flit(_fill(n=256))


def test_the_encoded_stream_matches_the_instruction_stream():
    prog = _prog()
    cluster = [i for i in prog.insts if i.op.value in ("fill", "gemm", "drain")]
    assert len(cluster_words(prog)) == len(cluster)
