"""`.ktpu` L2 bodies: every hand-written `fn NAME.l2` reads and verifies, and
the verifier refuses each rule's breach at its statement."""

import pytest
from kohakuaccel.text import module
from kohakuaccel.text.syntax import TextError
from kohakutpu.ktpu.l2 import nodes as L
from kohakutpu.ktpu.l2 import reader, verify

from kohakutpu import ktpu

UNITS = {"VC": 2, "MG": 4}
HAND = sorted(
    (p, name)
    for p in ktpu.KERNELS.rglob("*.ktpu")
    for name, level in ktpu.load(p).fns
    if level == "l2"
)


def check(text: str):
    m = module.read(text)
    (name,) = m.names()
    body = reader.body(m, name)
    verify.check(body, UNITS)
    return body


@pytest.mark.parametrize("path, name", HAND, ids=[f"{n}" for _, n in HAND])
def test_hand_l2_verifies(path, name):
    body = reader.body(ktpu.load(path), name)
    verify.check(body, UNITS)
    assert body.stmts


def test_every_fused_kernel_has_an_l2_body():
    names = {n for _, n in HAND}
    assert {"mlp", "swiglu", "attention", "flash"} <= names


HEAD = "fn k.l2(x: f16[256, 64] @dram, y: f16[256, 64] @dram)\n"


def test_reads_the_statements():
    body = check(
        HEAD + """    t = buffer                                  : f16[256, 64] @dram
    par u in 0..2 on vc[u]
        for i in 0..4 pipe=2
            xt = load x[u*128 + i*32 : +32, :]      : f16[32, 64] @l1
            e = exp2 xt                             : f32[32, 64]
            store t[u*128 + i*32 : +32, :] <- mul e, 2.0
"""
    )
    buf, par = body.stmts
    assert buf == L.Buffer("t", L.Type("f16", (256, 64), "dram"))
    assert (par.var, par.lo, par.hi, par.unit) == ("u", 0, 2, "vc")
    (loop,) = par.body
    assert (loop.var, loop.hi, loop.pipe) == ("i", 4, 2)
    load, ex, store = loop.body
    assert load.type == L.Type("f16", (32, 64), "l1")
    assert ex.op == "exp2" and ex.type.shape == (32, 64)
    assert isinstance(store.value, L.Op) and store.value.args[1] == L.Const(2.0)


def test_loop_carried_value_keeps_its_type():
    check(HEAD + """    par u in 0..1 on vc[0]
        xt = load x[0 : +32, :]                     : f16[32, 64] @l1
        m = copy xt                                 : f32[32, 64]
        for i in 1..4
            xi = load x[i*32 : +32, :]              : f16[32, 64] @l1
            m = max m, xi                           : f32[32, 64]
        store y[0 : +32, :] <- m
""")


BAD = {
    "out of bounds": (
        """    par u in 0..2 on vc[u]
        xt = load x[u*200 : +64, :]                 : f16[64, 64] @l1
""",
        "axis 0: 200..264 outside 0..256",
    ),
    "unit index": (
        """    par u in 0..3 on vc[u]
        xt = load x[u*64 : +64, :]                  : f16[64, 64] @l1
""",
        "vc[2]: this machine has 2",
    ),
    "load not in l1": (
        """    par u in 0..1 on vc[0]
        xt = load x[0 : +64, :]                     : f16[64, 64]
""",
        "this load is f16[64, 64] @l1",
    ),
    "vector op on a cluster": (
        """    par u in 0..1 on mg[0]
        e = exp2 x                                  : f32[256, 64]
""",
        "`exp2` runs on vc",
    ),
    "memory as a register": (
        """    par u in 0..1 on vc[0]
        e = exp2 x                                  : f32[256, 64]
""",
        "x is memory: load it",
    ),
    "another unit's register": (
        """    par u in 0..2 on vc[u]
        xt = load x[u*128 : +128, :]                : f16[128, 64] @l1
        par c in 0..1 on vc[1 - u]
            e = exp2 xt                             : f32[128, 64]
""",
        "xt is another unit's",
    ),
    "gemm operand dtypes": (
        """    par u in 0..1 on mg[0]
        acc = gemm x, y k=64                        : f32[256, 256] @acc
""",
        "a gemm reads an mx7a A and an mx7b B",
    ),
    "loop-carried type change": (
        """    par u in 0..1 on vc[0]
        m = load x[0 : +32, :]                      : f16[32, 64] @l1
        for i in 1..4
            m = exp2 m                              : f32[32, 64]
""",
        "loop-carried m changes type",
    ),
    "shape annotation": (
        """    par u in 0..1 on vc[0]
        xt = load x[0 : +32, :]                     : f16[32, 64] @l1
        e = exp2 xt                                 : f32[32, 32]
""",
        "exp2 gives [32, 64], annotated [32, 32]",
    ),
    "undefined name": (
        """    par u in 0..1 on vc[0]
        e = exp2 z                                  : f32[32, 64]
""",
        "z is not defined here",
    ),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_verifier_refuses(case):
    text, message = BAD[case]
    with pytest.raises(
        TextError, match=message.replace("[", r"\[").replace("]", r"\]")
    ) as e:
        check(HEAD + text)
    assert e.value.line > 1
