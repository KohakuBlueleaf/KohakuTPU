"""`.ktpu` L1 images for the V2 vector core: the encoder's configuration
tracking, decoded field by field."""

import pytest
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.encode import vector as A
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError

STREAM = """
image k(n)
    seti s5 = n
    loop s5
        vunpk v0 <- l1[0] f16
        vmul v1, v0, v0
        vpack v1 -> l1[64] f16
    halt
"""


def op(w: int) -> int:
    return w >> 27


def image(text: str, name: str = "k", args=(), entry=vector.UNKNOWN) -> list:
    return vector.image(module.read(text), name, args, entry)[0]


def test_unknown_entry_sets_the_loop_state_once():
    """Entered in an unknown state, a loop of unpack, math and pack sets
    VL+CHK, USTR and PSTR once before it, and its body is unchanged."""
    reset = image(STREAM, args=(8,), entry=vector.RESET)
    unknown = image(STREAM, args=(8,))
    loop = next(i for i, w in enumerate(reset) if op(w) == A.VLOOP)
    assert unknown[:loop] == reset[:loop]
    assert sorted(unknown[loop : loop + 3]) == sorted(
        [A.chkvl(128), A.cfg_stride(A.CFG_USTR, 1, 4), A.cfg_stride(A.CFG_PSTR, 1, 4)]
    )
    assert unknown[loop + 3 :] == reset[loop:]


# ---------------------------------------------------------------- math
def test_operand_fields_follow_the_op():
    w = image("""
image k()
    vmul v16, v0, s0
    vadd v16, v16, k1
    vfma v1, v2, v3, v4
    vexp2d v5, k0, v6
    halt
""")
    assert w[1:5] == [
        A.math("vmul", 16, 0, 0, sb=A.SRC_S),
        A.math("vadd", 16, 16, 0, 1, sc=A.SRC_K),
        A.math("vfma", 1, 2, 3, 4),
        A.math("vexp2d", 5, 0, 6, sa=A.SRC_K),
    ]
    assert w[0] == A.chkvl(128)


def test_config_words_only_on_change():
    w = image("""
image k()
    vmax v1[4:], v2[0:], v2[4:] vl=64
    vmax v1[4:], v2[0:], v2[4:] vl=64
    vmax v1[0:], v3[0:], v3[4:] vl=64
    vmul v9, v9, s0 vl=64
    halt
""")
    cfg = [x for x in w if op(x) == A.VCFG]
    assert cfg == [
        A.chkvl(64, (0, 1), (4, 1), (0, 1), (4, 1)),
        A.xb(),
        A.chkvl(64, (0, 1), (4, 1), (0, 1), (0, 1)),
    ]
    assert sum(1 for x in w if op(x) == A.OPS["vmax"]) == 3


def test_free_fields_keep_the_state():
    """VMAX reads no c: its c nib keeps what the last op set."""
    w = image("""
image k()
    vadd v1[0:], v2[0:], v2[4:] vl=64
    vmax v1[0:], v2[0:], v2[4:] vl=64
    halt
""")
    assert w[0] == A.chkvl(64, (0, 1), (0, 1), (4, 1), (0, 1))
    assert w[3] == A.chkvl(64, (0, 1), (4, 1), (4, 1), (0, 1))


def test_crossbar_on_c_reads_b():
    w = image("""
image k()
    vadd v21, v20, merge(v20[4:], 4) vl=64
    vsub v3, v3, bcast(v22[2], lane=5, sh=3)
    halt
""")
    assert w[1] == A.xb(A.XM_MERGE, 4, 0, True)
    assert w[2] == A.math("vadd", 21, 20, 20, 20, om=True)
    assert w[3] == A.chkvl(128, (0, 1), (2, 0), (0, 1), (0, 1))
    assert w[4] == A.xb(A.XM_BCAST, 5, 3, True)
    assert w[5] == A.math("vsub", 3, 3, 22, 22, om=True)


def test_predicates_and_compares():
    w = image("""
image k()
    vcmpgt p2, v26, s2 vl=32
    vadd v24, v24, v26 vl=32 if=p2
    halt
""")
    assert A.xb(A.XM_NONE, 0, 0, False, 0, 2) in w
    assert A.math("vcmpgt", 0, 26, 2, sb=A.SRC_S, om=True) in w
    assert A.xb(A.XM_NONE, 0, 0, False, 1, 2) in w


# ------------------------------------------------------- control, misc
def test_loop_counts_its_body_and_meets_states():
    """The body ends at VL 16, starts needing 128: every pass pays a CHKVL."""
    w = image(
        """
image k(n)
    seti s5 = n
    loop s5
        vmul v1, v1, v1
        vmul v2, v2, v2 vl=16
    halt
""",
        args=(3,),
    )
    assert w[:2] == [A.vseti(5), 3]
    head = w.index(A.vloop(5, 4))
    assert w[head + 1 :] == [
        A.chkvl(128),
        A.math("vmul", 1, 1, 1),
        A.chkvl(16),
        A.math("vmul", 2, 2, 2),
        A.vhalt(),
    ]


def test_skipz_and_scalars():
    w = image("""
image k()
    seti s0 = -1.5
    seti k3 = 2.0
    getstk s3
    skipz s3
        vbar q=1
        ainc a6 += -6K
    halt
""")
    assert w == [
        A.vseti(0),
        A.e8m15(-1.5),
        A.vseti(0, to_k=True),
        A.e8m15(2.0),
        A.getstk(3),
        A.vskipz(3, 2),
        A.vbar(1),
        A.ainc(6, -6144),
        A.vhalt(),
    ]


def test_walks_dma_and_sync():
    w = image("""
image k()
    vunpk v4[2:] <- l1[96] f16 n=2 rel
    vunpk v5 <- l1[8] gt4 walk=(1, 16) q=1
    vpack v6 -> l1[256] mx7b
    vfill a0 -> l1[0] rel
    vdrain a2 <- l1[256] rel to=(1, 2) buf=1 signal
    vsync drain mark=m5
    vmark m5 on=pack1 slack=1
    halt
""")
    assert w == [
        # The increment outlives a RUN: a `rel` transfer of an image that
        # sets none steps by 0, not by what an earlier image left.
        A.ainc(0, 0),
        A.ainc(2, 0),
        A.cfg_stride(A.CFG_USTR, 1, 4),
        A.vunpk(4, 96, 2, A.F16, 2, rel=True),
        A.cfg_stride(A.CFG_USTR, 1, 16),
        A.vunpk(5, 8, 8, A.GT4, q=1),
        A.cfg_stride(A.CFG_PSTR, 1, 4),
        A.vpack_mx7(6, 256, b_layout=True),
        A.vfill(0, 0, rel=True),
        A.vdrain(2, 256, rel=True, node=(1, 2), buf_id=1, signal=True),
        A.vsync(A.W_D, mark=5),
        A.vmark(5, A.K_PACK1, 1),
        A.vhalt(),
    ]


def test_rel_keeps_a_stated_ainc():
    w = image("""
image k()
    ainc a1 += 512
    seti s0 = 4
    loop s0
        vfill a1 -> l1[0] rel
        vfill a1 -> l1[64] rel
    halt
""")
    assert w[0] == A.ainc(1, 512)
    assert A.ainc(1, 0) not in w


@pytest.mark.parametrize(
    "line, said",
    [
        ("vmul v32, v0, v1", "v0..v31"),
        ("vadd v1, v2", "3 operands"),
        ("vsync pack", "on=KIND or mark"),
        ("vexp2d v1, xor(v2, 1), v3", "crossbar is on b"),
        ("vfoo v1", "no V2 instruction"),
        ("loop s1\n        loop s2\n            halt", "does not nest"),
    ],
)
def test_errors_name_the_line(line, said):
    with pytest.raises(TextError, match=said) as e:
        image(f"image k()\n    {line}\n")
    assert e.value.line == 2 + line.count("\n") // 2
