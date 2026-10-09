"""KohakuTPU's L1 words (docs/projects/kohakutpu/ir/text.md §1) on the
framework's L1 text (`kohakuaccel.ir.l1.text`): one word a hardware op, every
field its encoding carries printed, so a text builds the package of the
program it was written from. A vector-core image is a top-level ``image imgN``
block of instructions whose sources carry their selector (v, s, c, k)."""

import re

from kohakuaccel.ir.l1.text import L1Text
from kohakuaccel.text.syntax import Arrow, Assign, Dims, Name, Stmt
from kohakuaccel.text.vocab import Vocabulary, dec, hexa, tup
from kohakutpu.hw import vector as V
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.model import MACHINES
from kohakutpu.ir.l1.mover import Copy, Quantise
from kohakutpu.ir.l1.program import Program
from kohakutpu.ir.l1.vector import (
    Alu,
    Bar,
    Desc,
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
from kohakutpu.ir.l1.vector import Dims as DimsOp

TEXT = L1Text(Program)
MG = TEXT.unit("MG")
VC = TEXT.unit("VC")
#: A vector core's instructions, the lines of an ``image`` block.
CODE = Vocabulary("vector core instruction")

SIDES = "AB"
SELECTORS = {V.SRC_V: "v", V.SRC_S: "s", V.SRC_C: "c", V.SRC_K: "k"}
SELECTOR_OF = {v: k for k, v in SELECTORS.items()}
MODES = {V.FLAT: "flat", V.D2: "d2", V.D4: "d4", V.TREE: "tree"}
MODE_OF = {v: k for k, v in MODES.items()}
DTYPES = {V.DT_FP16: "f16", V.DT_FP32: "f32"}
DTYPE_OF = {v: k for k, v in DTYPES.items()}
MNEMONIC = {code: op for op, code in V.OPS.items()}
#: Opcodes with a word of their own; every other one is an `Alu` line.
OWN = {
    "VLD",
    "VST",
    "VSHUF",
    "VSETVL",
    "VSETMODE",
    "VSETI",
    "VLOOP",
    "VBAR",
    "VFILL",
    "VDRAIN",
    "VHALT",
}

_REG = re.compile(r"([A-Za-z])(\d+)$")


def _reg(r, st, t, kinds: str) -> tuple:
    """``(letter, index)`` of a register term such as ``v3`` or ``a1``."""
    m = _REG.match(r.name(st, t))
    if m is None or m.group(1) not in kinds:
        r.fail(st, f"wanted a register of {', '.join(kinds)}; got {t!r}")
    return m.group(1), int(m.group(2))


def _args(r, st, n: int) -> list:
    pos = st.positional()
    if len(pos) != n:
        r.fail(st, f"`{st.op}` takes {n} operands, got {len(pos)}")
    return pos


def _known(r, st, allowed) -> dict:
    kw = st.kwargs()
    extra = set(kw) - set(allowed)
    if extra:
        r.fail(st, f"`{st.op}` has no {', '.join(sorted(extra))}=")
    return kw


def _pred(op) -> list:
    return [Assign(Name(k), dec(v)) for k, v in (("pr", op.pr), ("pm", op.pm)) if v]


# ------------------------------------------------------------------ cluster
@MG.writes(Fill)
def _(w, op):
    if op.sel not in (0, 1):
        raise ValueError(f"a FILL of side {op.sel}: L1 has sides 0 and 1")
    args = [Name(f"{SIDES[op.sel]}{op.fbank}"), Arrow("<-"), hexa(op.addr)]
    args.append(Assign(Name("n"), dec(op.n)))
    if op.eoff:
        args.append(Assign(Name("l1"), dec(op.eoff)))
    return Stmt("fill", args)


@MG.reads("fill")
def _(r, st):
    side, arrow, addr = _args(r, st, 3)
    letter, bank = _reg(r, st, side, "AB")
    if arrow != Arrow("<-"):
        r.fail(st, "wanted `fill SIDEBANK <- ADDRESS`")
    kw = _known(r, st, ("n", "l1"))
    if "n" not in kw:
        r.fail(st, "a fill wants n=")
    return Fill(
        r.int(st, addr),
        r.int(st, kw["n"]),
        sel=SIDES.index(letter),
        eoff=r.int(st, kw.get("l1", dec(0))),
        fbank=bank,
    )


@MG.writes(Gemm)
def _(w, op):
    args = [Dims((op.gm, op.gn, op.nk))]
    if op.acc:
        args.append(Name("acc"))
    args += [
        Assign(Name("a"), Name(f"A{op.abank}")),
        Assign(Name("b"), Name(f"B{op.bbank}")),
    ]
    for k in ("aoff", "boff"):
        if getattr(op, k):
            args.append(Assign(Name(k), dec(getattr(op, k))))
    if op.emit:
        args.append(Assign(Name("emit"), hexa(op.addr)))
    return Stmt("gemm", args)


@MG.reads("gemm")
def _(r, st):
    pos = st.positional()
    if not pos or not isinstance(pos[0], Dims) or len(pos[0].values) != 3:
        r.fail(st, "wanted `gemm GMxGNxNK`")
    if pos[1:] not in ([], [Name("acc")]):
        r.fail(st, "after the shape a gemm takes only `acc`")
    kw = _known(r, st, ("a", "b", "aoff", "boff", "emit"))
    gm, gn, nk = pos[0].values
    banks = {
        k: _reg(r, st, kw.get(k, Name(f"{k.upper()}0")), k.upper())[1] for k in "ab"
    }
    return Gemm(
        gm,
        gn,
        nk,
        acc=len(pos) == 2,
        aoff=r.int(st, kw.get("aoff", dec(0))),
        boff=r.int(st, kw.get("boff", dec(0))),
        abank=banks["a"],
        bbank=banks["b"],
        emit="emit" in kw,
        addr=r.int(st, kw["emit"]) if "emit" in kw else 0,
    )


@MG.writes(Drain)
def _(w, op):
    args = [hexa(op.addr)]
    if op.fuse:
        args.append(Name("fused"))
    args.append(Assign(Name("n"), dec(op.n)))
    if op.dst is not None:
        args += [
            Assign(Name("to"), tup(*op.dst)),
            Assign(Name("flags"), dec(op.dflags)),
            Assign(Name("ack"), tup(*op.ack)),
        ]
    elif op.dflags or tuple(op.ack) != (0, 0):
        raise ValueError("a drain to memory carries no flags and no ack")
    return Stmt("drain", args)


@MG.reads("drain")
def _(r, st):
    pos = st.positional()
    if not pos or pos[1:] not in ([], [Name("fused")]):
        r.fail(st, "wanted `drain ADDRESS [fused] n=N`")
    kw = _known(r, st, ("n", "to", "flags", "ack"))
    if "n" not in kw:
        r.fail(st, "a drain wants n=")
    node = {}
    if "to" in kw:
        node = {
            "dst": r.ints(st, kw["to"]),
            "dflags": r.int(st, kw.get("flags", dec(0))),
            "ack": r.ints(st, kw.get("ack", tup(0, 0))),
        }
    elif "flags" in kw or "ack" in kw:
        r.fail(st, "flags= and ack= belong to a drain to=(x, y)")
    return Drain(r.int(st, pos[0]), r.int(st, kw["n"]), fuse=len(pos) == 2, **node)


# --------------------------------------------------------- vector core sends
@VC.writes(Image)
def _(w, op):
    def make(name):
        return Stmt(
            "image", [Name(name)], body=[s for i in op.code for s in CODE.write(w, i)]
        )

    name = w.define(("img", op.code), "img", make)
    return Stmt("load", [Name(name), Name("at"), dec(op.at)])


@VC.reads("load")
def _(r, st):
    pos = st.positional()
    if len(pos) != 3 or pos[1] != Name("at"):
        r.fail(st, "wanted `load IMAGE at WORD`")
    name = r.name(st, pos[0])
    code = r.names.get(name)
    if not isinstance(code, tuple):
        r.fail(st, f"no image {name!r}")
    return Image(code, r.int(st, pos[2]))


@VC.writes(Desc)
def _(w, op):
    return Stmt("desc", [Name(f"a{op.ad}"), hexa(op.base)])


@VC.reads("desc")
def _(r, st):
    ad, base = _args(r, st, 2)
    return Desc(_reg(r, st, ad, "a")[1], r.int(st, base))


@VC.writes(DimsOp)
def _(w, op):
    return Stmt("dims", [Name(f"a{op.ad}"), *(tup(s, b) for s, b in op.dims)])


@VC.reads("dims")
def _(r, st):
    pos = st.positional()
    if not pos:
        r.fail(st, "wanted `dims aN (stride, bound) ...`")
    walks = []
    for t in pos[1:]:
        pair = r.ints(st, t)
        if len(pair) != 2:
            r.fail(st, f"a dimension is (stride, bound), got {pair}")
        walks.append(pair)
    return DimsOp(_reg(r, st, pos[0], "a")[1], tuple(walks))


@VC.writes(Run)
def _(w, op):
    return Stmt("run", [dec(op.pc)])


@VC.reads("run")
def _(r, st):
    (pc,) = _args(r, st, 1)
    return Run(r.int(st, pc))


# --------------------------------------------------- vector core instructions
def _src(sel: int, idx: int) -> Name:
    return Name(f"{SELECTORS[sel]}{idx}")


@CODE.writes(Alu)
def _(w, op):
    mnem = op.op if isinstance(op.op, str) else MNEMONIC[op.op]
    args = [
        Name(f"v{op.vd}"),
        _src(op.sa, op.va),
        _src(op.sb, op.vb),
        _src(op.sc, op.vc),
    ]
    return Stmt(mnem.lower(), args + _pred(op), commas=True)


def _alu(r, st):
    vd, a, b, c = _args(r, st, 4)
    kw = _known(r, st, ("pr", "pm"))
    srcs = [_reg(r, st, t, "vsck") for t in (a, b, c)]
    return Alu(
        st.op.upper(),
        _reg(r, st, vd, "v")[1],
        *(i for _, i in srcs),
        *(SELECTOR_OF[k] for k, _ in srcs),
        pr=r.int(st, kw.get("pr", dec(0))),
        pm=r.int(st, kw.get("pm", dec(0))),
    )


CODE.reads(*(op.lower() for op in V.OPS if op not in OWN))(_alu)


@CODE.writes(Vshuf)
def _(w, op):
    args = [Name(f"v{op.vd}"), Name(f"v{op.va}"), Name(f"s{op.srot}")]
    return Stmt("vshuf", args + _pred(op), commas=True)


@CODE.reads("vshuf")
def _(r, st):
    vd, va, s = _args(r, st, 3)
    kw = _known(r, st, ("pr", "pm"))
    return Vshuf(
        _reg(r, st, vd, "v")[1],
        _reg(r, st, va, "v")[1],
        _reg(r, st, s, "s")[1],
        pr=r.int(st, kw.get("pr", dec(0))),
        pm=r.int(st, kw.get("pm", dec(0))),
    )


@CODE.writes(Vld, Vst)
def _(w, op):
    reg = op.vd if isinstance(op, Vld) else op.vs
    args = [Name(f"v{reg}"), Name(f"a{op.ad}"), dec(op.off)]
    if op.dt != V.DT_FP16:
        args.append(
            Assign(Name("dt"), Name(DTYPES[op.dt]) if op.dt in DTYPES else dec(op.dt))
        )
    return Stmt("vld" if isinstance(op, Vld) else "vst", args, commas=True)


@CODE.reads("vld", "vst")
def _(r, st):
    v, ad, off = _args(r, st, 3)
    kw = _known(r, st, ("dt",))
    dt = kw.get("dt", Name("f16"))
    dt = DTYPE_OF.get(dt.text) if isinstance(dt, Name) else r.int(st, dt)
    if dt is None:
        r.fail(st, f"no dtype {kw['dt']!r}; f16 or f32")
    cls = Vld if st.op == "vld" else Vst
    return cls(_reg(r, st, v, "v")[1], _reg(r, st, ad, "a")[1], r.int(st, off), dt)


@CODE.writes(Vfill)
def _(w, op):
    return Stmt("vfill", [Name(f"a{op.ad}"), dec(op.l1)], commas=True)


@CODE.reads("vfill")
def _(r, st):
    ad, l1 = _args(r, st, 2)
    return Vfill(_reg(r, st, ad, "a")[1], r.int(st, l1))


@CODE.writes(Vdrain)
def _(w, op):
    args = [Name(f"a{op.ad}"), dec(op.l1)]
    if op.node is not None:
        args.append(Assign(Name("node"), tup(*op.node)))
    if op.buf:
        args.append(Assign(Name("buf"), dec(op.buf)))
    if op.signal:
        args.append(Assign(Name("signal"), dec(1)))
    return Stmt("vdrain", args, commas=True)


@CODE.reads("vdrain")
def _(r, st):
    ad, l1 = _args(r, st, 2)
    kw = _known(r, st, ("node", "buf", "signal"))
    return Vdrain(
        _reg(r, st, ad, "a")[1],
        r.int(st, l1),
        node=r.ints(st, kw["node"]) if "node" in kw else None,
        buf=r.int(st, kw.get("buf", dec(0))),
        signal=bool(r.int(st, kw.get("signal", dec(0)))),
    )


@CODE.writes(Seti)
def _(w, op):
    reg = Name(f"{'k' if op.to_k else 's'}{op.sreg}")
    # Past 16 bits an immediate is a bit pattern (an e8m15 constant), not a count.
    imm = hexa(op.imm) if op.to_k or op.imm >= 1 << 16 else dec(op.imm)
    return Stmt("seti", [reg, imm], commas=True)


@CODE.reads("seti")
def _(r, st):
    reg, imm = _args(r, st, 2)
    kind, idx = _reg(r, st, reg, "sk")
    return Seti(idx, r.int(st, imm), to_k=kind == "k")


@CODE.writes(Setvl)
def _(w, op):
    return Stmt("setvl", [Name(f"s{op.sreg}")])


@CODE.reads("setvl")
def _(r, st):
    (reg,) = _args(r, st, 1)
    return Setvl(_reg(r, st, reg, "s")[1])


@CODE.writes(Setmode)
def _(w, op):
    return Stmt("setmode", [Name(MODES[op.mode]) if op.mode in MODES else dec(op.mode)])


@CODE.reads("setmode")
def _(r, st):
    (mode,) = _args(r, st, 1)
    if isinstance(mode, Name):
        if mode.text not in MODE_OF:
            r.fail(st, f"no mode {mode.text!r}; {', '.join(MODE_OF)}")
        return Setmode(MODE_OF[mode.text])
    return Setmode(r.int(st, mode))


@CODE.writes(Loop)
def _(w, op):
    return Stmt("loop", [Name(f"s{op.sreg}"), dec(op.body)], commas=True)


@CODE.reads("loop")
def _(r, st):
    reg, body = _args(r, st, 2)
    return Loop(_reg(r, st, reg, "s")[1], r.int(st, body))


@CODE.writes(Bar, Halt)
def _(w, op):
    return Stmt("bar" if isinstance(op, Bar) else "halt")


@CODE.reads("bar", "halt")
def _(r, st):
    _args(r, st, 0)
    return Bar() if st.op == "bar" else Halt()


@TEXT.defs.reads("image")
def _(r, st):
    if not st.body:
        r.fail(st, "an image with no instruction")
    return tuple(CODE.read(r, s) for s in st.body)


# -------------------------------------------------------------------- mover
@TEXT.mover.writes(Quantise, Copy)
def _(w, op):
    q = isinstance(op, Quantise)
    size = Assign(
        Name("entries" if q else "bytes"), dec(op.entries if q else op.nbytes)
    )
    return Stmt(
        "quantise" if q else "copy", [hexa(op.src), Arrow("->"), hexa(op.dst), size]
    )


@TEXT.mover.reads("quantise", "copy")
def _(r, st):
    src, arrow, dst = _args(r, st, 3)
    if arrow != Arrow("->"):
        r.fail(st, f"wanted `{st.op} SOURCE -> DESTINATION`")
    key = "entries" if st.op == "quantise" else "bytes"
    kw = _known(r, st, (key,))
    if key not in kw:
        r.fail(st, f"a {st.op} wants {key}=")
    cls = Quantise if st.op == "quantise" else Copy
    return cls(r.int(st, src), r.int(st, dst), r.int(st, kw[key]))


def write(programs, machine=None) -> str:
    """L1 text for one `Program` or a list of them."""
    return TEXT.write(programs, machine)


def read(text: str, machine=None, file: str = "<l1>") -> list:
    """The `Program`s of an L1 text, on `machine` or the machine it names."""
    return TEXT.read(text, machine, MACHINES, file)


__all__ = ["CODE", "MG", "TEXT", "VC", "read", "write"]
