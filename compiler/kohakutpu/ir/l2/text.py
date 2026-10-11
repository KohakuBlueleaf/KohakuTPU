"""KohakuTPU's L2 words (docs/projects/kohakutpu/ir/text.md §2) on the
framework's L2 text (`kohakuaccel.ir.l2.text`): its layouts and its item kinds
with the parameters each lowerer (`lowerers.py`) reads."""

from kohakuaccel.ir.l2.text import L2Text
from kohakutpu.ir.l1.model import MACHINES
from kohakutpu.ir.l2 import layouts

TEXT = L2Text()
for _cls in (
    layouts.MxA,
    layouts.MxB,
    layouts.Tiles,
    layouts.TileCols,
    layouts.ConvTiles,
    layouts.OnesA,
    layouts.BiasB,
    layouts.Flat,
    layouts.Rows,
    layouts.BandLane,
    layouts.ConvB,
):
    TEXT.layout(_cls)

_EPILOGUE = ("late", "ones_at", "bias_at")
TEXT.kind(
    "gemm_tile",
    "MG",
    ("a_at", "b_at", "c_at", "gm", "gn", "nk", "chunks"),
    _EPILOGUE,
)
TEXT.kind(
    "conv_tile",
    "MG",
    ("a_at", "a_chunk", "row0", "wp", "b_at", "c_at", "gm", "gn", "cbc", "chunks"),
    _EPILOGUE,
)
TEXT.kind("gemm", "MG", ("a", "b", "gm", "gn", "nk", "c_at"))
TEXT.kind(
    "vec_stream",
    "VC",
    ("body", "words", "runs", "step", "srcs", "dst"),
    ("sink", "resident_at", "install"),
)
TEXT.kind("vec_run", "VC", ("gm", "run", "p16_at", "o_at", "idx_at"), ("in_at",))
TEXT.kind("vec_prog", "VC", ("prog", "run", "ix_at"), ("in_at", "out_at", "ring"))
TEXT.kind("quantise", "mover", ("src", "dst", "entries"))
TEXT.kind("copy", "mover", ("src", "dst", "nbytes"))


def write(schedule, machine=None) -> str:
    """L2 text for a KohakuTPU schedule, on `machine` or the schedule's."""
    return TEXT.write(schedule, machine)


def read(text: str, machine=None, file: str = "<l2>"):
    """The `Schedule` of an L2 text, on `machine` or the machine it names."""
    return TEXT.read(text, machine, MACHINES, file)
