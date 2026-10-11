"""`.ktpu` L1 node bodies: an `fn NAME.l1` to the L1 `Program` its steps
name, against the framework's own unit ops."""

import pytest
from kohakuaccel.text import module
from kohakuaccel.text.syntax import TextError
from kohakutpu.hw import vector2 as A
from kohakutpu.ir.l1 import cluster as C
from kohakutpu.ir.l1 import mover as M
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ktpu.l1 import node, vector

from kohakutpu import ktpu

MACHINE = L1Model().machine


def program(text: str, binds=None):
    m = module.read(text)
    (name,) = m.names()
    return node.program(m, name, binds or {}, MACHINE)


def test_silu_sends_descriptors_image_and_run():
    m = ktpu.load(ktpu.kernel("silu"))
    p = node.program(m, "silu", {"x": 0x100000, "y": 0x200000}, MACHINE)
    vcs = p.units("VC")
    sends = [s for s in p.steps if s[0] == "send"]
    assert [s[1] for s in sends] == vcs[:2]
    for c, (_, _, flits, _) in enumerate(sends):
        words, descs = vector.image(
            m, "silu_stream", (8, 0x100000 + c * 65536, 0x200000 + c * 65536)
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
    assert p.steps[8] == ("fetch", p.units("VC")[0], 0x8000, 3)


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


@pytest.mark.parametrize("name, layers", [("mlp", 2), ("swiglu", 3)])
def test_fused_builds(name, layers):
    m = ktpu.load(ktpu.kernel(name))
    body = m.body(name, "l1")
    binds = {p: 0x100000 + i * 0x400000 for i, (p, _) in enumerate(body.params)}
    p = node.program(m, name, binds, MACHINE)
    sends = [s for s in p.steps if s[0] == "send"]
    runs = [s for s in sends if s[1] in p.units("VC")]
    assert len(runs) == 16  # one epilogue RUN per hidden tile
    gemms = [op for s in sends for op in s[3] if isinstance(op, C.Gemm)]
    assert len(gemms) == layers * 16 * 16  # 16 tiles a layer, 16 K-chunks a tile
    assert sum(g.emit for g in gemms) == layers * 16
    # Every emit lands on a distinct tile.
    assert len({g.addr for g in gemms if g.emit}) == layers * 16


def test_attention_builds():
    m = ktpu.load(ktpu.kernel("attention"))
    body = m.body("attention", "l1")
    binds = {p: 0x100000 + i * 0x400000 for i, (p, _) in enumerate(body.params)}
    p = node.program(m, "attention", binds, MACHINE)
    sends = [s for s in p.steps if s[0] == "send"]
    runs = [s for s in sends if s[1] in p.units("VC")]
    assert len(runs) == 8  # P and l, then o / l, per query tile
    ops = [op for s in sends for op in s[3]]
    gemms = [op for op in ops if isinstance(op, C.Gemm)]
    assert len(gemms) == 4 * (4 + 16)  # 4 score tiles and 16 P v chunks a cluster
    drains = [op for op in ops if isinstance(op, C.Drain)]
    s0, o0 = binds["s"], binds["o"]
    assert sorted(d.addr for d in drains if not d.fuse) == [
        s0 + t * 0x20000 for t in range(16)
    ]
    assert sorted(d.addr for d in drains if d.fuse) == [
        o0 + u * 0x8000 for u in range(4)
    ]
    assert sorted(g.addr for g in gemms if g.emit) == [
        o0 + u * 0x8000 for u in range(4)
    ]


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
