"""The L1 -> L1 image scheduler: dependences kept, fixed statements fixed,
independent work moved into a chain's latency."""

from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.opt import schedule as S
from kohakutpu.language.text import module

CHAIN = """image k()
    vunpk v0 <- l1[0] f16
    vexp2 v1, v0
    vexp2 v2, v1
    vexp2 v3, v2
    vunpk v8 <- l1[8] f16
    vmul v9, v8, v8
    vmul v10, v8, v8
    vsync pack on=drain
    vpack v3 -> l1[16] f16
    vpack v9 -> l1[24] f16
    halt
"""


def order(text) -> list:
    m = S.schedule_module(module.read(text))
    return [s for s in m.images["k"].stmts]


def test_a_chain_waits_and_independent_work_fills_the_wait():
    ops = [s.op for s in order(CHAIN)]
    # The second unpack and its multiplies move between the dependent exp2s.
    first_exp, last_exp = ops.index("vexp2"), len(ops) - 1 - ops[::-1].index("vexp2")
    between = ops[first_exp:last_exp]
    assert "vmul" in between
    # The sync stays where it was written: after every math op, before packs.
    sync = ops.index("vsync")
    assert all(o != "vpack" for o in ops[:sync])
    assert ops.count("vexp2") == 3 and ops[-1] == "halt"


def test_dependences_hold():
    stmts = order(CHAIN)
    names = [s.op for s in stmts]
    # v0 unpacked before its first reader, each exp2 after its source.
    reads = []
    for s in stmts:
        if s.op == "vexp2":
            reads.append((s.args[0].text, s.args[1].text))
    assert reads == [("v1", "v0"), ("v2", "v1"), ("v3", "v2")]
    assert names.index("vunpk") < names.index("vexp2")


def test_an_l1_word_is_not_read_before_its_pack():
    text = """image k()
    vpack v1 -> l1[0] f16
    vexp2 v5, v4
    vexp2 v6, v5
    vunpk v2 <- l1[0] f16
    halt
"""
    ops = [s.op for s in order(text)]
    assert ops.index("vpack") < ops.index("vunpk")


def test_a_scheduled_image_still_encodes():
    m = S.schedule_module(module.read(CHAIN))
    words, _ = vector.image(m, "k", ())
    assert words
