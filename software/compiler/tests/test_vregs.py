"""A V2 core's register count (32 / 64 / 128): the target line, the word
encoding past bit 32, the IMEM flit's pad, the verifier's decode, and the
L2 pipeline carrying the machine's count."""

import pathlib
import re

import pytest

from kohakuaccel.compiler.isa import ISAError
from kohakutpu.compiler import imem
from kohakutpu.compiler import target as T
from kohakutpu.compiler.emit import image as vector
from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.l2 import pipeline
from kohakutpu.compiler.verify.vector import _decode
from kohakutpu.language import examples
from kohakutpu.language.text import module as M
from kohakutpu.language.text.syntax import TextError

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_target_line_reads_and_writes_vregs():
    m = M.read("target ktpu.v9 vregs=128\n")
    assert (m.target, m.vregs) == ("ktpu.v9", 128)
    assert m.target_line() == "target ktpu.v9 vregs=128"
    assert M.read("target ktpu.v9\n").target_line() == "target ktpu.v9"
    with pytest.raises(TextError):
        M.read("target ktpu.v9 vregs=48\n")


def test_math_word_puts_each_field_past_bit_5_above_the_word():
    lo = A.math("vfma", 100 & 31, 65 & 31, 3, 127 & 31)
    want = lo | (127 >> 5) << 32 | (3 >> 5) << 34 | (65 >> 5) << 36 | (100 >> 5) << 38
    assert A.math("vfma", 100, 65, 3, 127, regs=128) == want
    # 64 registers: one bit a field, {vd, va, vb, vc} from bit 32.
    assert A.math("vfma", 33, 34, 2, 63, regs=64) >> 32 == 0b1101
    with pytest.raises(ISAError):
        A.math("vadd", 40, 1, 2, regs=32)


def test_unpack_register_rides_where_vd_does():
    assert A.vunpk(70, 8, regs=128) >> 32 == (70 >> 5) << 6


def test_imem_flit_carries_the_word_past_bit_32():
    word = A.math("vfma", 100, 65, 3, 127, regs=128)
    payload = VEC.imem(5, word)
    assert (payload >> 32) & 0xFF == word >> 32
    assert VEC.imem_word(payload) == word
    with pytest.raises(ISAError):
        VEC.imem(0, 1 << 40)


def test_resident_imem_rebase_keeps_the_word_past_bit_32():
    word = A.math("vfma", 100, 65, 3, 127, regs=128)
    flits = [A.imem_flit(0, word), A.run_flit(0)]
    # a placed program's IMEM flits are rebuilt at their slot's addresses
    got = imem.rewrite(flits, imem.Resident(imem.IMEM_WORDS))
    loads = [w for w in got if imem.op(w) == imem.OP_IMEM]
    assert [VEC.imem_word(w & imem.PAYLOAD) for w in loads] == [word]


def test_verifier_rejoins_register_fields():
    word = A.math("vfma", 100, 65, 3, 127, regs=128)
    _, f = _decode(word, 2)
    assert (f["vd"], f["va"], f["vb"], f["vc"]) == (100, 65, 3, 127)
    _, f = _decode(A.vunpk(70, 8, regs=128), 2)
    assert f["reg"] == 70


def test_image_encodes_for_the_target_core():
    text = "target ktpu.v9 vregs={}\n\nimage k()\n    vmul v100, v65, v3\n    halt\n"
    words, _ = vector.image(M.read(text.format(128)), "k", ())
    assert words[-2] == A.math("vmul", 100, 65, 3, regs=128)
    with pytest.raises(TextError):
        vector.image(M.read(text.format(32)), "k", ())


def test_each_core_build_names_the_count_and_copies_the_compiler_targets():
    # The build's VEC_CORE defines (v2_core.v RW 5 and RF_COPIES 4 unless set).
    cfg = (ROOT / "scripts" / "tcl" / "v8t3" / "00_config.tcl").read_text(
        encoding="utf-8"
    )
    for core, (regs, copies) in T.CORES.items():
        if core == "v2":
            defs = ""
        else:
            defs = re.search(
                rf"V8_VEC_CORES {core} .*?define \{{([^}}]*)\}}", cfg, re.DOTALL
            )[1]
        rw = re.search(r"V2_RW=(\d+)", defs)
        rc = re.search(r"V2_RF_COPIES=(\d+)", defs)
        assert (2 ** int(rw[1]) if rw else 32, int(rc[1]) if rc else 4) == (
            regs,
            copies,
        ), core
    m = T.machine(core="v21")
    assert (T.vregs(m), T.vcopies(m)) == (128, 2)


def test_pipeline_carries_the_machine_count():
    m = examples.load(examples.example("softmax", "l2"))
    l1 = pipeline.compile_l1(m, "softmax", T.machine(nodes=1, vregs=128))
    assert l1.vregs == 128
    assert l1.source.splitlines()[0] == "target ktpu.v9 vregs=128"
    l1 = pipeline.compile_l1(m, "softmax", T.machine(nodes=1))
    assert (l1.vregs, l1.source.splitlines()[0]) == (32, "target ktpu.v9")
