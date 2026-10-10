"""The vector-lane model against words `vec_alu.v` produced.

`lane_golden.npz` is RTL output captured by `lane_rtl.py` (Verilator, the
shipping PIPE_MUX build): per opcode, words where exact-then-round disagrees
with the RTL, and a random draw. A model that drifts from the datapath -- a
seed evaluated exactly, the MUL tie rule, the exp2 wrap -- fails here.
"""

import pathlib
import re

import numpy as np
import pytest
from kohakutpu.model import ALU_OP, LANE_PRED, SEED_TABLE, e8_value, lane, to_e8m15

HERE = pathlib.Path(__file__).resolve().parent
ROM = HERE.parents[2] / "src/kohakutpu/vector/vec_tables.v"
GOLD = np.load(HERE / "lane_golden.npz")


@pytest.mark.parametrize("op", sorted(ALU_OP, key=ALU_OP.get))
def test_every_golden_word(op):
    at = GOLD["o"] == ALU_OP[op]
    assert at.sum() >= 256, f"{op}: the golden file holds {at.sum()} vectors"
    a, b, c = (GOLD[k][at].astype(np.int64) for k in "abc")
    got, pred = lane(op, a, b, c)
    want = GOLD["r"][at].astype(np.int64)
    bad = np.flatnonzero(got != want)
    assert not len(bad), [
        f"{a[i]:06x} -> {got[i]:06x} != {want[i]:06x}" for i in bad[:4]
    ]
    if op in LANE_PRED:
        assert (pred == GOLD["p"][at]).all()


@pytest.mark.parametrize("op", ["VEXP2", "VLOG2", "VINV", "VRSQRT", "VMUL"])
def test_the_golden_words_tell_rtl_from_exact_rounding(op):
    """The negative control: exact-then-round fails these, so they can fail."""
    at = GOLD["o"] == ALU_OP[op]
    a, b = (e8_value(GOLD[k][at].astype(np.int64)) for k in "ab")
    with np.errstate(all="ignore"):
        exact = {
            "VEXP2": np.exp2(a),
            "VLOG2": np.log2(a),
            "VINV": 1.0 / a,
            "VRSQRT": 1.0 / np.sqrt(a),
            "VMUL": a * b,
        }[op]
    old = np.float32(to_e8m15(exact))
    new = e8_value(GOLD["r"][at].astype(np.int64)).astype(np.float32)
    assert ((old != new) & ~(np.isnan(old) & np.isnan(new))).sum() >= 100


def test_the_seed_rom_is_vec_tables_v():
    """The regenerated coefficients against the ROM the RTL reads."""
    text = ROM.read_text(encoding="utf-8")
    hexw = r"22'h([0-9a-f]+)"
    rows = re.findall(rf"8'd(\d+)\s*: begin w <= \{{{hexw}, {hexw}, {hexw}\}}", text)
    assert len(rows) == 32 * 3 + 64
    for addr, *words in rows:
        c2, c1, c0 = (int(w, 16) - ((int(w, 16) >> 21) << 22) for w in words)
        assert tuple(SEED_TABLE[int(addr) >> 6, int(addr) & 63]) == (c0, c1, c2), addr


def test_exp2_wraps_past_128_as_the_rtl_does():
    """|x| in [128, 256) leaves the 25-bit s8.17 word: RTL behaviour, kept."""
    x = np.array([0x434800, 0xC34800, 0x430000])  # 200, -200, 128
    got, _ = lane("VEXP2", x, 0, 0)
    assert list(e8_value(got)) == [2.0**-56, 2.0**56, 0.0]
