"""A `skipz` block past VSKIPZ's skip field on the card model (L1.IMG.027).

The block holds `n` one-word adds of 1.0 to v1, which starts at 0.0 and is
packed and drained after the block: y is 0.0 in every lane when the register
is zero and n when it is not. The encoder's split of the block must give the
same y as the block written by hand as consecutive `skipz` blocks of at most
128 adds, and as that count.
"""

import numpy as np
import pytest

from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

LANES = 128


def program(n, reg, parts):
    """`n` adds under `skipz s1` with s1 = `reg`, as consecutive blocks of
    `parts` adds each (`parts` None: one block)."""
    size = parts or n
    blocks = ""
    for start in range(0, n, size):
        adds = min(size, n - start)
        blocks += "    skipz s1\n" + "        vadd v1, v1, k1\n" * adds
    return (
        "target ktpu.v9\nimage sk(y)\n"
        "    desc a0 = y walk=(32,128)\n"
        f"    vmov v1, k0\n    seti s1 = {reg}\n" + blocks + "    vsync fe on=math\n"
        "    vpack v1 -> l1[0] f16\n    vsync fe on=pack\n"
        "    vdrain a0 <- l1[0]\n    halt\n"
        "fn sk.l1(y: f16[2048] @dram)\n"
        "    send vc[0]\n        run sk(y)\n    barrier\n"
    )


def run(dev, text):
    c = K.compile(text, name="sk", machine=dev.machine, level="l1")
    assert not c.verdict["refused"], c.verdict
    y = dev.alloc(4096)
    try:
        dev.write(y, bytes(4096))
        dev.run(c, {"y": y})
        out = dev.read(y, 2 * LANES)
    finally:
        dev.free(y)
    return np.frombuffer(out, np.float16).copy()


@pytest.mark.parametrize("reg", [0, 3])
@pytest.mark.parametrize("n", [255, 256, 300])
def test_a_long_skip_equals_the_block_split_by_hand(dev, n, reg):
    y = run(dev, program(n, reg, None))
    by_hand = run(dev, program(n, reg, 128))
    want = np.full(LANES, n if reg else 0, np.float16)
    assert (y == want).all(), (n, reg, y[:4])
    assert (by_hand == want).all(), (n, reg, by_hand[:4])


def test_a_later_part_without_its_head_runs_on_a_zero_register(dev):
    """With only the first part under VSKIPZ, a zero register still runs the
    rest of the block: y is the count of the adds past the first part, which
    fills the skip field."""
    with rs.mutant("l1.img/skipz-head-once"):
        y = run(dev, program(300, 0, None))
    assert (y == 300 - 255).all(), y[:4]
