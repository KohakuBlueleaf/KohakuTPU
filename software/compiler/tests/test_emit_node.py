"""`.ktpu` L1 node bodies: an `fn NAME.l1` to the L1 `Program` its steps
name, against the framework's own unit ops."""

import pytest
from kohakutpu.compiler import target
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.emit import node
from kohakutpu.compiler.encode import cluster as C
from kohakutpu.compiler.encode import mover as M
from kohakutpu.compiler.encode import vector as A
from kohakutpu.language.text import module
from kohakutpu.language.text.syntax import TextError

MACHINE = target.machine()

STREAM = """
image stream(n, x, y)
    desc a0 = x walk=(32, 128)
    desc a1 = y walk=(32, 128)
    vfill a0 -> l1[0]
    seti s5 = n
    loop s5
        vunpk v0 <- l1[0] f16
        vmul v1, v0, v0
        vpack v1 -> l1[128] f16
    vdrain a1 <- l1[128]
    halt

fn k.l1(x: f16[65536] @dram, y: f16[65536] @dram)
    for c in 0..2
        send vc[c]
            run stream(8, x + c*64K, y + c*64K)
    barrier
"""


def program(text: str, binds=None):
    m = module.read(text)
    (name,) = m.names()
    return node.program(m, name, binds or {}, MACHINE)


def test_a_core_gets_descriptors_image_and_run():
    m = module.read(STREAM)
    p = node.program(m, "k", {"x": 0x100000, "y": 0x200000}, MACHINE)
    vcs = p.units("VC")
    sends = [s for s in p.steps if s[0] == "send"]
    assert [s[1] for s in sends] == vcs[:2]
    for c, (_, _, flits, _, _) in enumerate(sends):
        words, descs = vector.image(
            m, "stream", (8, 0x100000 + c * 65536, 0x200000 + c * 65536)
        )
        want = [A.desc_flit(ad, f, v) for (ad, f), v in sorted(descs.items())]
        want += [A.imem_flit(i, w) for i, w in enumerate(words)]
        assert flits == want + [A.run_flit(0)]
    assert p.steps[-1] == ("barrier",)


def test_cluster_forms():
    p = program(
        """
fn k.l1(a: f16[64] @dram)
    send mg[1]
        fill A1 <- a + 16K n=128 at=4
        gemm 64x64x2 a=A1 b=B0 acc=1 emit=a
        gemm 64x64x2 a=A0 b=B1
        drain a n=4096 fused
        drain n=16 to=vc[0] l1=256 ack=(0, 1)
""",
        {"a": 0x1000},
    )
    (send,) = [s for s in p.steps if s[0] == "send"]
    mg = p.units("MG")[1]
    assert send[1] == mg
    assert list(send[3]) == [
        C.Fill(0x1000 + 16384, 128, sel=0, eoff=4, fbank=1),
        C.Gemm(64, 64, 2, acc=True, abank=1, bbank=0, emit=True, addr=0x1000),
        C.Gemm(64, 64, 2, abank=0, bbank=1),
        C.Drain(0x1000, 4096, fuse=True),
        C.Drain(256 * 32, 16, dst=p.units("VC")[0], ack=(0, 1)),
    ]


def test_marks_waits_moves_fetch():
    p = program(
        """
fn k.l1(a: f16[64] @dram, b: f16[64] @dram)
    for i in 0..2
        send mg[i]
            fill B0 <- a n=8
        t{i} = mark mg[i]
    wait mg[1] upto=t1
    move
        copy a -> b bytes=4K
        quant a -> b entries=4
    m = move posted
        gt4 a -> b groups=2
    wait moves m
    fetch vc[0] from b n=3
    barrier
""",
        {"a": 0x1000, "b": 0x8000},
    )
    kinds = [s[0] for s in p.steps]
    assert kinds == [
        "send",
        "mark",
        "send",
        "mark",
        "wait",
        "move",
        "post",
        "wait_moves",
        "fetch",
        "barrier",
    ]
    assert p.steps[4] == ("wait", p.units("MG")[1], 1)
    assert p.steps[5][2] == (
        M.Copy(0x1000, 0x8000, 4096),
        M.Quantise(0x1000, 0x8000, 4),
    )
    assert p.steps[6][2] == (M.Transpose4(0x1000, 0x8000, 2),)
    assert p.steps[8] == ("fetch", p.units("VC")[0], 0x8000, 3, ())


@pytest.mark.parametrize(
    "line, said",
    [
        ("send vc[9]\n        run k(1)", "this machine has"),
        ("wait mg[0] upto=nope", "names a mark"),
        ("wait moves nope", "move posted"),
        ("send mg[0]\n        gemm 64x64x2 a=B0 b=B1", "A bank"),
        ("send mg[0]\n        gemm 64x64x3 a=A0 b=B0", "pumped sweep"),
        ("frob", "no L1 node statement"),
        ("move\n        copy a -> a", "bytes=N"),
    ],
)
def test_errors(line, said):
    with pytest.raises(TextError, match=said):
        program(f"fn k.l1(a: f16[64] @dram)\n    {line}\n", {"a": 0x1000})


@pytest.mark.parametrize(
    "body, said",
    [
        ("gemm 64x64x2 a=A0 b=B0\n        drain a n=4096 fused", "none is open"),
        ("gemm 64x64x2 a=A0 b=B0 acc emit=a", "no fused drain collects"),
        (
            "gemm 64x64x2 a=A0 b=B0 acc emit=a\n        drain a n=1024 fused",
            "streams 4096",
        ),
    ],
)
def test_fused_drain_pairs_with_an_emit(body, said):
    with pytest.raises(TextError, match=said):
        program(
            f"fn k.l1(a: f16[64] @dram)\n    send mg[0]\n        {body}\n",
            {"a": 0x1000},
        )
