"""The data-movement forms the tinygrad runner's moves, transposes and flips
lower through, witnessed against the L1 text they must write: signed walk
copies and fills, walked GT4 moves, the lane crossbar, and one image a
distinct vector body."""

import re

import kohakutpu.language.dsl as kt
from kohakutpu.compiler import kernel as K
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import lower
from kohakutpu.language.text import module

MACHINE = T.machine(nodes=1)


def l1_of(src: str, name: str) -> str:
    return lower.lower(module.read(src), name, MACHINE)


def test_a_row_flip_is_one_copy_walking_back():
    text = l1_of(
        """target ktpu.v9

fn flip.l2(x: f16[8, 256] @dram, y: f16[8, 256] @dram)
    copy y <- x walk=((8, -512), (16, 32)) at=3584
""",
        "flip",
    )
    # x's last row first (byte 3584), rows 512 bytes back; y in order.
    assert "copy w(x + 3584, (8, -512), (16, 32)) -> w(y + 0, (128, 32))" in text


def test_a_window_fill_walks_the_destination():
    text = l1_of(
        """target ktpu.v9

fn pad.l2(y: f16[8, 64] @dram)
    fill y value=15360 ew=16 walk=((8, 128), (2, 32)) at=64
""",
        "pad",
    )
    # Columns 32..63 of every row (bytes 64.. of each 128-byte row).
    assert "fill w(y + 64, (8, 128), (2, 32)) value=15360 ew=16" in text


def test_a_walked_gt4_groups_four_source_words():
    text = l1_of(
        """target ktpu.v9

fn tr.l2(x: f16[16, 16] @dram, y: f16[16, 16] @dram)
    gt4 y <- x walk=((4, 128), (4, 32)) dwalk=((4, 128),)
""",
        "tr",
    )
    # Four row groups, each four consecutive words (rows) to four words.
    assert re.search(r"gt4 w\(x \+ 0, \(16, 32\)\) -> w\(y \+ 0, \(4, 128\)\)", text)


@kt.kernel
def swap(x=kt.In(16, 128), y=kt.Out(16, 128)):
    for c in kt.par(2, on=kt.vc):
        for t in kt.range(1):
            r = c * 8 + t * 8
            v = kt.unpack(kt.load(x[r : r + 8, :]))
            kt.store(y[r : r + 8, :], kt.xlane(v, 5))


def test_xlane_is_a_multiply_by_one_through_the_crossbar():
    c = K.specialise(swap, {"x": ((16, 128), "f16")}, machine=MACHINE)
    assert "xlane" in c.l2_text
    assert re.search(r"vmul v\d+, k1, xor\(v\d+, 5\)", c.l1_text)


def test_instances_of_one_shape_share_one_image():
    # Both cores run the same body: one image, each run binding its bases.
    c = K.specialise(swap, {"x": ((16, 128), "f16")}, machine=MACHINE)
    assert c.l1_text.count("\nimage ") == 1
    runs = re.findall(r"run (\w+)\(([^)]*)\)", c.l1_text)
    assert len(runs) == 2 and runs[0][0] == runs[1][0] and runs[0][1] != runs[1][1]
