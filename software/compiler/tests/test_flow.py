"""Unit-to-unit flow control in L1: the cluster's `recv` FILL, `credit`
drains and POST, the vector core's `vwait` / `vpost` and COUNT peer drains.
Every word is checked against the RTL's bit positions (mx_cluster_cu.v's
header, v2_core.v's decode), not against the encoder that made it."""

import pytest

from kohakutpu.compiler import target
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.emit import node
from kohakutpu.compiler.encode import cluster as C
from kohakutpu.compiler.encode import vector as A
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError

MACHINE = target.machine()


def bits(word: int, hi: int, lo: int) -> int:
    return (word >> lo) & ((1 << (hi - lo + 1)) - 1)


def cluster_words(body: str) -> list:
    m = module.read(f"fn k.l1(a: f16[64] @dram)\n    send mg[0]\n{body}")
    p = node.program(m, "k", {"a": 0x1000}, MACHINE)
    (send,) = [s for s in p.steps if s[0] == "send"]
    return [op.flits()[0] for op in send[3]], p


def test_cluster_recv_fill_credit_drain_post():
    words, p = cluster_words(
        "        fill B1 recv n=24\n"
        "        gemm 1x128x1 a=A0 aoff=32 b=B0 boff=128 acc emit to=vc[0] l1=128 count credit\n"
        "        drain n=128 to=vc[0] l1=128 fused count credit\n"
        "        post n=96 to=vc[1]\n"
    )
    fill, gemm, drain, post = words
    vc0, vc1 = p.units("VC")[:2]
    # FILL: op 1, n 24, sel B, bank 1, recv [141], no address.
    assert bits(fill, 255, 252) == 1 and bits(fill, 217, 202) == 24
    assert bits(fill, 201, 201) == 1 and bits(fill, 112, 112) == 1
    assert bits(fill, 141, 141) == 1 and bits(fill, 251, 218) == 0
    # The emitting GEMM and its fused DRAIN carry the same sink: dnode [111],
    # dst [110:103], dflags [94:87] = COUNT (2), credit [62], granule 128.
    for w in (gemm, drain):
        assert bits(w, 111, 111) == 1
        assert (bits(w, 110, 107), bits(w, 106, 103)) == vc0
        assert bits(w, 94, 87) == C.DFLAG_COUNT == 2
        assert bits(w, 62, 62) == 1 and bits(w, 141, 141) == 0
        assert bits(w, 251, 218) == 128 * 32
    assert bits(gemm, 255, 252) == 2 and bits(gemm, 116, 116) == 1
    assert bits(drain, 255, 252) == 3 and bits(drain, 115, 115) == 1
    # POST: op 4, n [217:202], dst [110:103].
    assert bits(post, 255, 252) == 4 and bits(post, 217, 202) == 96
    assert (bits(post, 110, 107), bits(post, 106, 103)) == vc1


def test_a_fused_drain_must_name_its_emits_sink():
    with pytest.raises(TextError, match="open emit"):
        cluster_words(
            "        gemm 1x128x1 a=A0 b=B0 acc emit to=vc[0] l1=128 count credit\n"
            "        drain n=128 to=vc[0] l1=128 fused count\n"
        )


def test_count_and_credit_need_a_unit():
    with pytest.raises(TextError, match="to=UNIT"):
        cluster_words("        drain a n=16 count\n")


IMG = """
image flow(h)
    desc a0 = peer(64, count=1) walk=(32, 8)
    vwait unpack rx n=128
    vwait drain cr n=40 q=1
    vpost (1 + h, 0) n=300
    vdrain a0 <- l1[0] to=(2, 1)
    halt
"""


def test_vwait_vpost_and_a_count_peer_drain():
    m = module.read(IMG)
    words, descs = vector.image(m, "flow", (1,))
    rx, cr, post, drain, _ = words
    # VSYNC op 0x16, waiter [26:24], kind 7 [23:21], n [20:11], q [10],
    # counter [9] (0 landed, 1 credits).
    assert bits(rx, 31, 27) == 0x16 and bits(rx, 23, 21) == 7
    assert (bits(rx, 26, 24), bits(rx, 20, 11), bits(rx, 9, 9)) == (A.W_U, 128, 0)
    assert (bits(cr, 26, 24), bits(cr, 20, 11), bits(cr, 10, 10), bits(cr, 9, 9)) == (
        A.W_D,
        40,
        1,
        1,
    )
    # VPOST op 0x19: dst_x [26:23], dst_y [22:19], n [15:0].
    assert bits(post, 31, 27) == 0x19
    assert (bits(post, 26, 23), bits(post, 22, 19), bits(post, 15, 0)) == (2, 0, 300)
    # A COUNT peer drain: descriptor base bit 34 (v2_core.v E_MEM0, nd_count).
    assert descs[(0, 0)] == 64 | 1 << 34
    assert bits(drain, 31, 27) == 0x1E


def test_a_count_is_not_marked():
    with pytest.raises(TextError):
        vector.image(
            module.read("image x()\n    vwait mark rx n=1\n    halt\n"), "x", ()
        )
