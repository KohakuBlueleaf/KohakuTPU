"""L2 text: every hand-written schedule printed and read back compiles to the
L1 its original compiles to, package for package; a hand-written text reads
to the schedule its Python transcription builds."""

import pathlib

import pytest
from kohakuaccel.ir.l2 import Schedule
from kohakuaccel.text.syntax import TextError
from kohakutpu.ir import l2
from kohakutpu.ir.l1 import text as l1_text
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.model import machine
from kohakutpu.ir.l2 import text as T
from kohakutpu.ir.l2.layouts import BandLane, ConvB, Flat, MxA, MxB, Rows, Tiles

GOLDEN = pathlib.Path(__file__).with_name("golden")
MACHINE = machine()
MGS = sorted(MACHINE.units["MG"])
VCS = sorted(MACHINE.units["VC"])


class Rig:
    """A schedule whose buffers are placed the way `L1Model.alloc` places them."""

    def __init__(self) -> None:
        self.s = Schedule(machine=MACHINE)
        self.top = 0x8010_0000

    def buf(self, name, layout, nbytes=None):
        n = nbytes if nbytes is not None else layout.nbytes
        at = -(-self.top // 256) * 256
        self.top = at + n
        return self.s.buffer(name, n, layout, base=at)


def schedule(name) -> Schedule:
    r = Rig()
    match name:
        case "matmul_bias":
            a, b = r.buf("a", MxA(128, 128, 8, 2)), r.buf("b", MxB(128, 128, 8, 2))
            c = r.buf("c", Tiles(128, 128, 8, 8))
            ones = r.buf("ones", None, 8 * MM.ENTRY)
            bias = r.buf("bias", None, 32 * MM.ENTRY)
            l2.ops.matmul(r.s, MGS, a, b, c, late=False, ones=ones, bias=bias)
        case "conv":
            x, w = r.buf("x", BandLane(16, 16, 64, 12, 2)), r.buf(
                "w", ConvB(32, 64, 8, 2)
            )
            c = r.buf("c", Tiles(CV.geometry(16, 16, 12)[2] * CV.LANES, 32, 12, 8))
            l2.ops.conv2d(r.s, MGS, x, w, c)
        case "silu_binary":
            x, z, y, s = (r.buf(n, Flat(16384)) for n in ("x", "z", "y", "sum"))
            l2.ops.silu(r.s, VCS, x, y, 16384)
            l2.ops.binary(r.s, VCS, "add", y, z, s, 16384, package=1)
        case "softmax_layernorm":
            x, sm, ln = (r.buf(n, Rows(32 * 128, 128)) for n in ("x", "sm", "ln"))
            gb = r.buf("gb", Flat(256))
            l2.ops.softmax(r.s, VCS, x, sm, 32, 128)
            l2.ops.layernorm(r.s, VCS, sm, ln, gb, 32, 128)
        case "matmul_silu":
            a, b = r.buf("a", MxA(256, 128, 16, 2)), r.buf("b", MxB(256, 128, 16, 2))
            c = r.buf("c", Tiles(256, 256, 16, 16))
            sinks = [r.buf(f"sink{i}", Flat(128 * 16)) for i in range(2)]
            l2.ops.matmul_silu(r.s, MGS, VCS, a, b, c, sinks, late=True, words=128)
        case "attention":
            q = r.buf("q", MxA(32, 64, 8, 2))
            k, v = r.buf("k", None, 3 * 4096), r.buf("v", None, 3 * 4096)
            o = r.buf("o", Tiles(32, 64, 8, 16))
            scratch, idx = r.buf("scratch", None, 4 * 8 * 16 * 32), r.buf(
                "idx", Flat(32)
            )
            l2.ops.attention(r.s, MGS[0], VCS[0], q, k, v, o, scratch, idx, 8, 3)
    return r.s


SCHEDULES = [
    "matmul_bias",
    "conv",
    "silu_binary",
    "softmax_layernorm",
    "matmul_silu",
    "attention",
]


def packages(programs) -> list:
    return [p.build(None, {}).build(defaults=False).to_bytes() for p in programs]


@pytest.mark.parametrize("name", SCHEDULES)
def test_a_schedule_printed_and_read_compiles_to_the_same_l1(name):
    s = schedule(name)
    text = T.write(s)
    back = T.read(text)
    assert T.write(back) == text, "the printed form is a fixed point"
    want, got = l2.compile(s), l2.compile(back)
    assert l1_text.write(got) == l1_text.write(want)
    assert packages(got) == packages(want)
    assert l2.lower(text) == l1_text.write(want), "text in, text out"


def transcribed() -> Schedule:
    """golden/schedule.l2, written in Python."""
    r = Rig()
    x, y = r.buf("x", Flat(8192)), r.buf("y", Flat(8192))
    a, b = r.buf("a", MxA(32, 64, 8, 2)), r.buf("b", MxB(64, 64, 16, 2))
    c = r.buf("c", Tiles(32, 64, 8, 16))
    p7 = r.buf("p7", None, 2048)
    l2.ops.silu(r.s, VCS, x, y, 8192, words=128)
    r.s.add(
        "quantise",
        "mover",
        {"src": y.base, "dst": p7.base, "entries": 64},
        reads=[y.view(0, 64 * 32)],
        writes=[p7.view()],
    )
    l2.ops.matmul(r.s, MGS[:1], a, b, c, package=1)
    return r.s


def test_a_hand_written_schedule_compiles_to_its_transcription():
    """Hand-written, not printed: the witness of the text is not the printer."""
    s = T.read((GOLDEN / "schedule.l2").read_text(encoding="utf-8"))
    want = transcribed()
    assert l1_text.write(l2.compile(s)) == l1_text.write(l2.compile(want))
    assert T.write(s) == T.write(want)


HEAD = 'level l2\nmachine "kohakutpu-l1"\nunit mg0 MG (1, 0)\nunit vc0 VC (1, 2)\n'


@pytest.mark.parametrize(
    "text,where,what",
    [
        (HEAD + "buffer x Nope(n=1) at 0x8010_0000\n", 5, "no layout 'Nope'"),
        (HEAD + "buffer x Flat(m=1)\n", 5, "unexpected keyword"),
        (HEAD + "buffer x at 0x8010_0000\n", 5, "wants bytes="),
        (HEAD + "buffer x Flat(n=8)\nbuffer x Flat(n=8)\n", 6, "named twice"),
        (HEAD + "package 0\n    item gemm on vc0\n", 6, "runs on a MG"),
        (
            HEAD + "package 0\n    item quantise on mover\n        with src=1\n",
            6,
            "missing",
        ),
        (
            HEAD + "buffer x Flat(n=8)\npackage 0\n    item quantise on mover\n"
            "        with src=x dst=1 entries=1\n",
            8,
            "has no address",
        ),
        (
            HEAD + "buffer x Flat(n=8) at 0x8010_0000\npackage 0\n"
            "    item quantise on mover\n        read y\n",
            8,
            "wanted a view",
        ),
    ],
)
def test_a_wrong_schedule_is_refused_at_its_line(text, where, what):
    with pytest.raises(TextError) as e:
        T.read(text)
    assert e.value.line == where
    assert what in e.value.message
