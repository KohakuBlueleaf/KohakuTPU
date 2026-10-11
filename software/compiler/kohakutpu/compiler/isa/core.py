"""The V2 vector core's 32-bit instruction words (`src/kohakutpu/vector2/v2_core.v`),
declared once: `encode.vector` packs through these formats and `CORE`
decodes them.

Every word is ``op[31:27]`` and its fields below; bits a format leaves unused
are zero-valued constants, so a decode rejects a word the encoder never makes.
A `VCFG` word has one format per selector. `VSETI` is followed by one more
word, its 24-bit immediate, which no format describes.
"""

from kohakuaccel.compiler.isa import Field, InstFormat, InstSet
from kohakutpu.language.l1.ops import MATH

#: Math opcodes, keyed by the L1 mnemonics (`kohakutpu.language.l1.ops`).
OPS = {
    "vmov": 0x00,
    "vneg": 0x01,
    "vabs": 0x02,
    "vadd": 0x03,
    "vsub": 0x04,
    "vmul": 0x05,
    "vfma": 0x06,
    "vfnma": 0x07,
    "vmax": 0x08,
    "vmin": 0x09,
    "vsel": 0x0A,
    "vcmplt": 0x0B,
    "vcmpgt": 0x0C,
    "vcmpeq": 0x0D,
    "vexp2": 0x0E,
    "vlog2": 0x0F,
    "vinv": 0x10,
    "vrsqrt": 0x11,
    "vexp2d": 0x12,
}
if set(OPS) != MATH:
    raise ImportError(
        f"the core's math opcodes {sorted(set(OPS) ^ MATH)} differ from "
        "kohakutpu.language.l1.ops.MATH: one is stale"
    )
VCFG, VUNPK, VPACK, VSYNC, VSKIPZ = 0x13, 0x14, 0x15, 0x16, 0x17
VSETVL, VSETI, VLOOP, VBAR = 0x18, 0x1A, 0x1B, 0x1C
VFILL, VDRAIN, VHALT = 0x1D, 0x1E, 0x1F

#: VCFG selectors.
CFG_CHK, CFG_XB, CFG_UPTR, CFG_UINC, CFG_USTR = 0, 1, 2, 3, 4
CFG_PPTR, CFG_PINC, CFG_PSTR, CFG_GETSTK, CFG_AINC, CFG_CHKVL = 5, 6, 7, 8, 9, 10

#: The VSYNC waiter that records a mark instead of waiting.
W_MK = 5

WORD = 32


def _op(code: int) -> Field:
    return Field("op", 5, const=code)


def _zero(name: str, width: int) -> Field:
    return Field(name, width, const=0)


def _f(name: str, width: int, **kw) -> Field:
    return Field(name, width, default=0, **kw)


def _fmt(name: str, *fields: Field, doc: str = "") -> InstFormat:
    return InstFormat(name, fields, width=WORD, doc=doc)


def _chunks() -> tuple:
    """The four operands' (base chunk, stride 0|1), d first; d is vd."""
    out = []
    for x in "dcba":
        out += [_f(f"{x}_base", 3), Field(f"{x}_stride", 1, default=1)]
    return tuple(out)


def _cfg(name: str, sel: int, *fields: Field, doc: str = "") -> InstFormat:
    return _fmt(name, _op(VCFG), Field("sel", 4, const=sel), *fields, doc=doc)


def _walk(name: str, code: int, doc: str) -> InstFormat:
    return _fmt(
        name,
        _op(code),
        Field("reg", 5),
        _f("mode", 2),
        Field("n1", 3, doc="chunks - 1"),
        _f("cb", 3),
        _f("rel", 1),
        _f("q", 1),
        Field("off", 12),
        doc=doc,
    )


MATHS = {
    op: _fmt(
        op.upper(),
        _op(code),
        Field("vd", 5),
        _f("va", 5),
        _f("vb", 5),
        _f("vc", 5),
        _f("sa", 2),
        _f("sb", 2),
        _f("sc", 2),
        _f("om", 1),
        doc="S/K operands name S[reg & 15] / K[reg & 3]",
    )
    for op, code in OPS.items()
}

CHK = _cfg("CHK", CFG_CHK, _zero("z", 7), *_chunks())
CHKVL = _cfg("CHKVL", CFG_CHKVL, Field("vl1", 7, doc="VL - 1"), *_chunks())
XB = _cfg(
    "XB",
    CFG_XB,
    _zero("z", 8),
    _f("pr", 2),
    _f("pm", 2),
    _f("xc", 1),
    _f("sh", 3),
    _f("xk", 4),
    _f("xm", 3),
)
POINTERS = {
    sel: _cfg(name, sel, _zero("z", 10), Field("ptr", 13))
    for name, sel in (
        ("UPTR", CFG_UPTR),
        ("UINC", CFG_UINC),
        ("PPTR", CFG_PPTR),
        ("PINC", CFG_PINC),
    )
}
STRIDES = {
    sel: _cfg(name, sel, _zero("z", 5), Field("s1", 9), Field("s0", 9))
    for name, sel in (("USTR", CFG_USTR), ("PSTR", CFG_PSTR))
}
GETSTK = _cfg("GETSTK", CFG_GETSTK, _zero("z", 19), Field("sreg", 4))
AINC = _cfg(
    "AINC", CFG_AINC, Field("ad", 3), _zero("z", 2), Field("inc", 18, signed=True)
)
#: Any selector, its payload raw: what a selector no format above names decodes to.
CFG = _fmt("VCFG", _op(VCFG), Field("sel", 4), Field("payload", 23))

UNPK = _walk("VUNPK", VUNPK, "vd chunks cb.. from L1 words off + i*USTR")
PACK = _walk("VPACK", VPACK, "L1 words off + i*PSTR from vs chunks cb..")
MARK = _fmt(
    "VMARK",
    _op(VSYNC),
    Field("waiter", 3, const=W_MK),
    Field("on", 3),
    _f("slack", 6),
    _zero("marked", 1),
    Field("mark", 3),
    _zero("q", 1),
    _zero("z", 10),
)
SYNC = _fmt(
    "VSYNC",
    _op(VSYNC),
    Field("waiter", 3),
    _f("on", 3),
    _f("slack", 6),
    _f("marked", 1),
    _f("mark", 3),
    _f("q", 1),
    _zero("z", 10),
)
SKIPZ = _fmt(
    "VSKIPZ",
    _op(VSKIPZ),
    _zero("z1", 1),
    Field("sreg", 4),
    _zero("z", 14),
    Field("skip", 8),
)
SETVL = _fmt("VSETVL", _op(VSETVL), _zero("z1", 6), Field("sreg", 4), _zero("z", 17))
SETI = _fmt(
    "VSETI",
    _op(VSETI),
    _zero("z1", 1),
    Field("sreg", 4),
    _zero("z", 21),
    _f("to_k", 1),
    doc="the next word is the 24-bit immediate",
)
LOOP = _fmt(
    "VLOOP",
    _op(VLOOP),
    _zero("z1", 6),
    Field("sreg", 4),
    Field("body", 10),
    _zero("z", 7),
)
BAR = _fmt("VBAR", _op(VBAR), _zero("z1", 16), _f("q", 1), _zero("z", 10))
HALT = _fmt("VHALT", _op(VHALT), _zero("z", 27))
FILL = _fmt(
    "VFILL",
    _op(VFILL),
    _f("rel", 1),
    _zero("z1", 2),
    Field("ad", 3),
    _zero("z", 7),
    _f("off", 14),
)
DRAIN = _fmt(
    "VDRAIN",
    _op(VDRAIN),
    _f("rel", 1),
    _f("signal", 1),
    _f("node", 1),
    Field("ad", 3),
    _f("dst_x", 4),
    _f("dst_y", 4),
    _f("buf_id", 4),
    _f("off", 9),
)

#: Every format, the most constrained first where two share an opcode.
CORE = InstSet(
    "kohakutpu-v2",
    [
        *MATHS.values(),
        CHK,
        CHKVL,
        XB,
        *POINTERS.values(),
        *STRIDES.values(),
        GETSTK,
        AINC,
        CFG,
        UNPK,
        PACK,
        MARK,
        SYNC,
        SKIPZ,
        SETVL,
        SETI,
        LOOP,
        BAR,
        HALT,
        FILL,
        DRAIN,
    ],
)

__all__ = ["CORE", "MATHS", "OPS"]
