"""DSL kernel + tensor specs -> L2 -> L1 (`compiler.kernel`), and the L2
vector forms the DSL's row kernels lower through: rows-mode GT4 bands of
row-major buffers, row broadcasts, statistics, f32 rows."""

import itertools
import re

import numpy as np
import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.compiler import kernel as K
from kohakutpu.compiler import target as T
from kohakutpu.language.examples import dsl as ex

MACHINE = T.machine(nodes=1)


@pytest.mark.parametrize("name", sorted(ex.EXAMPLES))
def test_each_example_compiles_to_l1(name):
    e = ex.EXAMPLES[name]
    c = K.specialise(e.kernel, e.given, machine=MACHINE, **e.consts)
    assert c.l1_text.count("image ") >= 1 and "halt" in c.l1_text
    assert (
        dict(c.params)[e.out].shape
        == e.reference(e.inputs(np.random.default_rng(0))).shape
    )


def test_a_second_specialisation_compiles_nothing():
    e = ex.EXAMPLES["softmax"]
    a = K.specialise(e.kernel, e.given, machine=MACHINE, **e.consts)
    b = K.specialise(e.kernel, e.given, machine=MACHINE, **e.consts)
    assert a is b


@kt.kernel
def wide(x=kt.In("R", "W"), y=kt.Out("R", "W"), *, rows=kt.auto(64, 32, 16)):
    per = x.shape[0] // 2
    for c in kt.par(2, on=kt.vc):
        for t in kt.range(per // rows):
            r = c * per + t * rows
            kt.store(y[r : r + rows, :], kt.exp2(kt.load(x[r : r + rows, :], gt4=True)))


def test_auto_takes_the_first_candidate_that_compiles():
    # 64 rows of 256 columns, double-buffered in and out, overflow a core's L1.
    c = K.specialise(wide, {"x": ((256, 256), "f16")}, machine=MACHINE)
    assert c.traced.consts["rows"] < 64


def _unpacked_and_read(l1: str) -> tuple:
    regs = set(re.findall(r"vunpk (v\d+) <-", l1))
    read = set()
    for ln in l1.splitlines():
        if ln.strip().startswith(("vadd", "vmax")):
            read |= set(re.findall(r"\b(v\d+)\b", ln.split(",", 1)[1]))
    return regs, read


@kt.kernel
def bcast_sum(x=kt.In("W"), y=kt.Out(16, 32)):
    for c in kt.par(1, on=kt.vc):
        v = kt.unpack(kt.load(x, gt4=True))
        for t in kt.range(1):
            kt.store(y[t * 16 : t * 16 + 4, :], kt.rsum(v))


@pytest.mark.parametrize("w", [64, 256])
def test_a_reduction_of_a_row_broadcast_reads_every_register(w):
    # Only a value made before the stream loop is read in it: the loop takes
    # its width from that value, so the tree covers all w / 32 registers.
    c = K.specialise(bcast_sum, {"x": ((w,), "f16")}, machine=MACHINE)
    regs, read = _unpacked_and_read(c.l1_text)
    assert len(regs) == w // 32 and regs <= read


@kt.kernel
def stat_use(
    x=kt.In("R", 128), m=kt.In("R", 32), s=kt.Out("R", 32), y=kt.Out("R", 128)
):
    per = x.shape[0] // 2
    for c in kt.par(2, on=kt.vc):
        for t in kt.range(per // 8):
            r = c * per + t * 8
            v = kt.load(x[r : r + 8, :], gt4=True)
            mm = kt.load(m[r : r + 8, :], gt4=True)
            kt.store(s[r : r + 8, :], kt.rmax(v))
            kt.store(y[r : r + 8, :], kt.exp2(v - mm))


def test_statistics_load_and_store_as_32_wide_bands():
    c = K.specialise(
        stat_use, {"x": ((64, 128), "f16"), "m": ((64, 32), "f16")}, machine=MACHINE
    )
    assert "f16[8, 32] @l1(gt4)" in c.l2_text  # the statistic's band
    assert c.l1_text.count("vpack") >= 2


@kt.kernel
def f32rows(x=kt.In("R", 128, dtype="f32"), y=kt.Out("R", 128, dtype="f32")):
    per = x.shape[0] // 2
    for c in kt.par(2, on=kt.vc):
        for t in kt.range(per // 8):
            r = c * per + t * 8
            kt.store(y[r : r + 8, :], kt.load(x[r : r + 8, :]) * 2.0 + 1.0)


def test_f32_rows_unpack_sixteen_words_a_register():
    c = K.specialise(f32rows, {"x": ((64, 128), "f32")}, machine=MACHINE)
    at = [
        int(a)
        for a in re.findall(r"vunpk v\d+ <- l1\[(\d+)\] f32$", c.l1_text, re.MULTILINE)
    ]
    assert len(at) >= 8 and {b - a for a, b in itertools.pairwise(at[:8])} == {16}
