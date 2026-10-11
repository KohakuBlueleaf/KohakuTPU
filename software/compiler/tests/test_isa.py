"""The declared KohakuTPU ISAs against the words the silicon has run.

`SILICON` and `CORE_SILICON` hold the hand-packed encoders' output for each
case, frozen when those encoders were retired; the bitstream executed exactly
these words, so they are the authority. A drifted field shows up here rather
than as traffic that routes plausibly and computes something else.
"""

import pytest
from kohakuaccel.compiler.isa import ISAError
from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa.cluster import ISA
from kohakutpu.compiler.isa.core import CORE
from kohakutpu.compiler.isa.vector import ISA as VEC

SILICON = {
    "fill_a": 0x1048D159E0010000000128000000000000000000000000000000000000000000,
    "fill_b": 0x1FFFFFFFFC03FE00000128000000000000000000000000000000000000000000,
    "fill_off": 0x1000000100001C00000128000000190000050000000000000000000000000000,
    "gemm": 0x2000000000000010201928000000000000000000000000000000000000000000,
    "gemm_acc": 0x2000000000000104040128000000000101820000000000000000000000000000,
    "gemm_emit": 0x2000000000000008080328000000000000180000000000000000000000000000,
    "drain": 0x337AB40000080000000128000000000000000000000000000000000000000000,
    "drain_node": 0x3000000000004000000128000000000000009181008080000000000000000000,
    "imem_0": 0x10000000000000000000000000000000000000000000000000000000DEADBEEF,
    "imem_max": 0x1FF80000000000000000000000000000000000000000000000000000FFFFFFFF,
    "desc": 0x2752345678900000000000000000000000000000000000000000000000000000,
    "desc_max": 0x2FFFFFFFFFF00000000000000000000000000000000000000000000000000000,
    "run_0": 0x3000000000000000000000000000000000000000000000000000000000000000,
    "run_pc": 0x3960000000000000000000000000000000000000000000000000000000000000,
}

CASES = [
    ("fill_a", dict(op=1, addr=0x1234_5678, n=64, sel=0)),
    ("fill_b", dict(op=1, addr=0x3_FFFF_FFFF, n=255, sel=1)),
    ("fill_off", dict(op=1, addr=0x40, n=7, sel=0, eoff=200, abank=1, fbank=1)),
    ("gemm", dict(op=2, gm=16, gn=32, nk=25)),
    ("gemm_acc", dict(op=2, gm=4, gn=4, nk=1, acc=True, aoff=8, boff=12, bbank=1)),
    ("gemm_emit", dict(op=2, gm=8, gn=8, nk=3, emit=True, fuse=True)),
    ("drain", dict(op=3, addr=0xDEAD_0000, n=512)),
    (
        "drain_node",
        dict(op=3, addr=0, n=16, dnode=True, dst=(2, 3), dbuf=2, dflags=1, dack=(1, 0)),
    ),
]

VEC_CASES = [
    ("imem_0", VEC.imem, {"addr": 0, "word": 0xDEADBEEF}),
    ("imem_max", VEC.imem, {"addr": 511, "word": 0xFFFFFFFF}),
    ("desc", VEC.desc, {"ad": 3, "fld": 5, "value": 0x1_2345_6789}),
    ("desc_max", VEC.desc, {"ad": 7, "fld": 7, "value": (1 << 34) - 1}),
    ("run_0", VEC.run, {"pc": 0}),
    ("run_pc", VEC.run, {"pc": 300}),
]


def declared(spec: dict) -> int:
    kw = dict(spec)
    op = kw.pop("op")
    dst = kw.pop("dst", (0, 0))
    dack = kw.pop("dack", (0, 0))
    kw.update(dst_x=dst[0], dst_y=dst[1], dack_x=dack[0], dack_y=dack[1], dfin=0)
    kw.update(ISA.peers(()))
    kw.setdefault("anchor", ISA.cfg.anchor)
    fmt = {1: ISA.FILL, 2: ISA.GEMM, 3: ISA.DRAIN}[op]
    return fmt.encode(**{k: int(v) for k, v in kw.items()})


def show(name, got, want) -> str:
    return f"{name}: declared {got:#068x}\n  silicon  {want:#068x}\n  xor {got ^ want:#068x}"


@pytest.mark.parametrize("name, spec", CASES, ids=[n for n, _ in CASES])
def test_cluster_isa_is_the_silicon_payload(name, spec):
    got = declared(spec)
    assert got == SILICON[name], show(name, got, SILICON[name])


@pytest.mark.parametrize("name, fn, kw", VEC_CASES, ids=[n for n, *_ in VEC_CASES])
def test_vector_isa_is_the_silicon_payload(name, fn, kw):
    got = fn(**kw)
    assert got == SILICON[name], show(name, got, SILICON[name])


#: The V2 core's words as the retired hand-shifted encoder made them (the one
#: the card ran): ``(encoder function, args, keywords, word)``.
CORE_SILICON = [
    ("math", ("vmul", 16, 0, 0), {"sb": 1}, 0x2C000008),
    ("math", ("vadd", 16, 16, 0, 1), {"sc": 3}, 0x1C200086),
    (
        "math",
        ("vfma", 31, 30, 29, 28),
        {"sa": 3, "sb": 1, "sc": 3, "om": True},
        0x37FDDE6F,
    ),
    ("math", ("vexp2d", 5, 0, 6), {"sa": 3}, 0x91406060),
    ("math", ("vrsqrt", 1, 2), {}, 0x88440000),
    ("chk", ((7, 1), (6, 0), (5, 1), (4, 0)), {}, 0x98008BCF),
    ("chkvl", (128,), {}, 0x9D7F1111),
    ("chkvl", (1, (0, 1), (2, 0), (4, 1), (7, 0)), {}, 0x9D00E941),
    ("chkvl", (64, (0, 1), (4, 1), (0, 1), (4, 1)), {}, 0x9D3F9191),
    ("xb", (), {}, 0x98800000),
    ("xb", (4, 4, 0, True), {}, 0x98800424),
    ("xb", (3, 15, 7, True, 3, 3), {}, 0x98807FFB),
    ("cfg_ptr", (2, 8191), {}, 0x99001FFF),
    ("cfg_ptr", (6, 77), {}, 0x9B00004D),
    ("cfg_stride", (4, 1, 4), {}, 0x9A000801),
    ("cfg_stride", (7, 511, 256), {}, 0x9B8201FF),
    ("cfg_stride", (4, -1, -4), {}, 0x9A03F9FF),
    ("ainc", (6, -6144), {}, 0x9CE3E800),
    ("ainc", (0, 4096), {}, 0x9C801000),
    ("ainc", (7, 131071), {}, 0x9CF1FFFF),
    ("getstk", (3,), {}, 0x9C000003),
    ("getstk", (15,), {}, 0x9C00000F),
    ("vunpk", (4, 96, 2, 0, 2), {"rel": True}, 0xA102A060),
    ("vunpk", (5, 8, 8, 1), {"q": 1}, 0xA15E1008),
    ("vunpk", (31, 4095, 1, 2, 7), {"rel": True, "q": 1}, 0xA7E1FFFF),
    ("vpack", (6, 256), {}, 0xA98E0100),
    ("vpack", (9, 17, 4, 1, 4), {"q": 1}, 0xAA571011),
    ("vpack_mx7", (6, 256), {"b_layout": True}, 0xA9BE4100),
    ("vpack_mx7", (2, 0), {"rel": True, "q": 1}, 0xA8BE3000),
    ("vsync", (6,), {"mark": 5}, 0xB6006800),
    ("vsync", (1, 3, 63), {"q": 1}, 0xB17F8400),
    ("vsync", (3, 6, 0), {}, 0xB3C00000),
    ("vmark", (5, 6, 1), {}, 0xB5C0A800),
    ("vmark", (7, 0, 63), {}, 0xB51FB800),
    ("vskipz", (3, 2), {}, 0xB8C00002),
    ("vskipz", (15, 255), {}, 0xBBC000FF),
    ("vsetvl", (9,), {}, 0xC0120000),
    ("vseti", (0,), {}, 0xD0000000),
    ("vseti", (15,), {"to_k": True}, 0xD3C00001),
    ("vloop", (5, 4), {}, 0xD80A0200),
    ("vloop", (15, 1023), {}, 0xD81FFF80),
    ("vbar", (), {}, 0xE0000000),
    ("vbar", (1,), {}, 0xE0000400),
    ("vhalt", (), {}, 0xF8000000),
    ("vfill", (0, 0), {"rel": True}, 0xEC000000),
    ("vfill", (7, 16383), {}, 0xE8E03FFF),
    (
        "vdrain",
        (2, 256),
        {"rel": True, "node": (1, 2), "buf_id": 1, "signal": True},
        0xF7424300,
    ),
    ("vdrain", (7, 511), {}, 0xF0E001FF),
    ("vdrain", (3, 0), {"node": (15, 15), "buf_id": 15}, 0xF17FFE00),
]


@pytest.mark.parametrize("fn, args, kw, word", CORE_SILICON)
def test_core_word_is_the_silicon_word(fn, args, kw, word):
    got = getattr(A, fn)(*args, **kw)
    assert got == word, f"{fn}{args} {kw}: {got:#010x}, silicon {word:#010x}"


CFG_NAMES = {2: "UPTR", 3: "UINC", 4: "USTR", 5: "PPTR", 6: "PINC", 7: "PSTR"}


def format_of(fn: str, args: tuple) -> str:
    """The format a decoder must find for encoder `fn`'s word."""
    if fn == "math":
        return args[0].upper()
    if fn in ("cfg_ptr", "cfg_stride"):
        return CFG_NAMES[args[0]]
    if fn == "vpack_mx7":
        return "VPACK"
    return fn.upper() if fn.startswith("v") else fn.upper().replace("_", "")


@pytest.mark.parametrize("fn, args, kw, word", CORE_SILICON)
def test_a_decoder_finds_the_encoders_format(fn, args, kw, word):
    fmt = CORE.find(word)
    assert fmt is not None and fmt.name == format_of(fn, args), CORE.disasm(word)
    assert fmt.encode(**fmt.decode(word)) == word


def test_a_word_too_wide_for_its_field_is_refused():
    for bad in (lambda: A.math("vadd", 32), lambda: A.vfill(8), lambda: A.chkvl(129)):
        with pytest.raises(ISAError):
            bad()


def test_vector_program_ends_in_run():
    flits = VEC.program([0x1111, 0x2222], pc=0)
    assert len(flits) == 3
    assert VEC.set.disasm(flits[-1]).startswith("RUN")


def test_fill_refuses_a_wrapping_count():
    ISA.fill(addr=0, n=255)
    with pytest.raises(ValueError):
        ISA.fill(addr=0, n=256)


def test_disassembly_names_the_opcode():
    assert ISA.set.disasm(ISA.gemm(gm=4, gn=4, nk=2)).startswith("GEMM")
