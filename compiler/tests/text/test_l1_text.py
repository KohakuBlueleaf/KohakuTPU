"""L1 text: every hand-written kernel printed, read back, builds the package it
was printed from, byte for byte; a hand-written text builds what the Python
program it transcribes builds."""

import pathlib

import pytest
from kohakuaccel.text.syntax import TextError
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1 import text as T
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import binary as BI
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import fused as FU
from kohakutpu.ir.l1.kernels import layernorm as LN
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ir.l1.mover import Copy, Quantise
from kohakutpu.ir.l1.vector import (
    Alu,
    Bar,
    Desc,
    Dims,
    Halt,
    Image,
    Loop,
    Run,
    Seti,
    Setmode,
    Setvl,
    Vdrain,
    Vfill,
    Vld,
    Vshuf,
    Vst,
)

GOLDEN = pathlib.Path(__file__).with_name("golden")
MACHINE = L1Model().machine
BASE = 0x8010_0000


def package(prog) -> bytes:
    return prog.build(None, {}).build(defaults=False).to_bytes()


def kernel(name):
    p = Program(MACHINE)
    a, b, c, d = BASE, BASE + 0x40_0000, BASE + 0x80_0000, BASE + 0xC0_0000
    match name:
        case "matmul":
            MM.matmul(p, a, b, c, 128, 128, 128, 8, 8, 2, stagger=True)
        case "linear_bias":
            MM.matmul(p, a, b, c, 64, 96, 256, 4, 8, 4, ones_at=d, bias_at=d + 0x1000)
        case "matmul_silu":
            sinks = [d, d + 0x1_0000]
            FU.matmul_silu(
                p, a, b, c, 256, 256, 128, 16, 16, 2, late=True, words=128, sinks=sinks
            )
        case "attention":
            AT.attention(p, a, b, c, d, d + 0x10_0000, d + 0x20_0000, 8, 3)
        case "conv":
            CV.conv(p, a, b, c, 16, 16, 64, 32, 12, 8, 2)
        case "silu":
            SI.silu(p, a, c, 16384, 256)
        case "binary":
            BI.binary(p, "sub", a, b, c, 16384)
        case "layernorm":
            LN.layernorm(p, a, c, b, 32, 320, 8)
        case "softmax":
            SM.softmax(p, a, c, 64, 96, 8)
    return p


KERNELS = [
    "matmul",
    "linear_bias",
    "matmul_silu",
    "attention",
    "conv",
    "silu",
    "binary",
    "layernorm",
    "softmax",
]


@pytest.mark.parametrize("name", KERNELS)
def test_a_kernel_printed_and_read_builds_its_package(name):
    prog = kernel(name)
    text = T.write(prog)
    (back,) = T.read(text, MACHINE)
    assert back.steps == prog.steps
    assert package(back) == package(prog)
    assert T.write(back) == text, "the printed form is a fixed point"


def test_programs_share_one_file_and_their_images():
    progs = [kernel("silu"), kernel("binary"), kernel("silu")]
    text = T.write(progs)
    back = T.read(text, MACHINE)
    assert [package(p) for p in back] == [package(p) for p in progs]
    assert text.count("\nimage ") == len(
        {
            op.code
            for p in progs
            for s in p.steps
            if s[0] == "send"
            for op in s[3]
            if isinstance(op, Image)
        }
    )


def transcribed() -> Program:
    """golden/words.l1, written in Python."""
    p = Program(MACHINE)
    mg0, mg1 = p.units("MG")[:2]
    vc0 = p.units("VC")[0]
    code = (
        Seti(0, 128),
        Seti(3, 0x3F8000, to_k=True),
        Setvl(0),
        Setmode(3),
        Vld(1, 1, 8),
        Vld(2, 1, 16, dt=2),
        Alu("VADD", 3, 1, 2, 3, sa=0, sb=1, sc=3, pr=1, pm=2),
        Alu("VEXP2", 3, 3, 3, 3),
        Vshuf(4, 3, 5, pr=2),
        Loop(1, 2),
        Vst(4, 2, 0),
        Vfill(1, 256),
        Bar(),
        Vdrain(3, 0, node=(2, 2), buf=1, signal=True),
        Vdrain(2, 64),
        Halt(),
    )
    p.send(mg0, Fill(BASE, 128), Fill(BASE + 0x1000, 64, sel=1, eoff=64, fbank=1))
    p.send(mg0, Gemm(8, 8, 2, abank=0, bbank=1), Drain(BASE + 0x2000, 64))
    p.send(
        mg1,
        Gemm(
            8, 8, 2, acc=True, aoff=16, boff=4, abank=1, emit=True, addr=BASE + 0x3000
        ),
    )
    t0 = p.mark(mg1)
    p.send(mg1, Drain(BASE + 0x3000, 64, fuse=True, dst=(1, 2), dflags=1, ack=(0, 1)))
    p.wait(mg1, t0)
    p.send(
        vc0,
        Image(code, 4),
        Desc(1, BASE),
        Dims(1, ((1, 8), (32, 4))),
        Dims(2, ()),
        Run(4),
    )
    p.wait(vc0)
    p.move(Quantise(BASE, BASE + 0x4000, 512), Copy(BASE + 0x4000, BASE + 0x8000, 4096))
    p.move([(0x10, 1), (0x14, 0xFF)])
    p.barrier()
    return p


def test_a_hand_written_text_builds_what_its_python_transcription_builds():
    """The text is written by hand, not by the printer: a witness of the words
    independent of the printer they would otherwise be checked against."""
    (prog,) = T.read((GOLDEN / "words.l1").read_text(encoding="utf-8"), MACHINE)
    want = transcribed()
    assert prog.steps == want.steps
    assert package(prog) == package(want)


HEAD = 'level l1\nmachine "kohakutpu-l1"\n'
MG0 = HEAD + "unit mg0 MG (1, 0)\nprogram p0\n"


@pytest.mark.parametrize(
    "text,where,what",
    [
        ("level l2\n", 1, "not `level l1`"),
        ('level l1\nmachine "nope"\n', 2, "the text is for 'nope'"),
        (HEAD + "unit mg0 MG (0, 0)\n", 3, "no MG at (0, 0)"),
        (HEAD + "program p0\n    send mg9\n", 4, "no unit 'mg9'"),
        (MG0 + "    send mg0\n        gemm 8x8x3\n", 6, "odd nk"),
        (MG0 + "    wait mg0 upto t4\n", 5, "no mark 't4'"),
        (
            HEAD
            + "unit vc0 VC (1, 2)\nprogram p0\n    send vc0\n        load img0 at 0\n",
            6,
            "no image 'img0'",
        ),
        (HEAD + "image img0\n    vadd v1, v2\n", 4, "takes 4"),
        (HEAD + "image img0\n    vadd v1, q2, v0, v0\n", 4, "register"),
        (HEAD + "program p0\n  barrier\n barrier\n", 5, "dedent"),
    ],
)
def test_a_wrong_text_is_refused_at_its_line(text, where, what):
    with pytest.raises(TextError) as e:
        T.read(text, MACHINE)
    assert e.value.line == where
    assert what in e.value.message
