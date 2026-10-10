"""SimDram's map, routing and Xache invalidation, against an in-memory model.

The fake holds what the card model exposes -- four `u_dram<h>.mem` arrays and
four homes' carray banks -- and records every row a write zeroes, so the tests
check bytes and invalidated rows, not just that a round trip agrees.
"""

import pytest
from kohakuaccel.transport.simdram import LINE, DramMap, SimDram

BOARD = {"dram_interleave_kb": 16, "dram_flat_gb": 16}
WORDS, ROWS, BANKS, ENT = 4096, 64, 4, 68
WINDOWS = [0x1_0000_0000, 0x2_0000_0000]


class FakeModel:
    """The public arrays of a 4-home card, and a pass-through log."""

    bulk = True

    def __init__(self) -> None:
        self.mem = {}
        for h in range(4):
            self.mem[(f"TOP.c.u_dram{h}", "mem")] = bytearray(WORDS * LINE)
            for b in range(BANKS):
                s = f"TOP.c.u_xache.u_kx.g_home[{h}].u_c.g_bank.g_b[{b}].u_arr.u_ram"
                self.mem[(s, "mem")] = bytearray(b"\xff" * (ROWS * ENT))
        self.passed = []

    def arrays(self, sub):
        return [
            (
                s,
                v,
                len(m) // (LINE if "dram" in s else ENT),
                LINE if "dram" in s else ENT,
            )
            for (s, v), m in self.mem.items()
            if sub in s
        ]

    def _ent(self, scope):
        return LINE if "dram" in scope else ENT

    def peek(self, scope, var, index, count):
        e = self._ent(scope)
        return bytes(self.mem[(scope, var)][index * e : (index + count) * e])

    def poke(self, scope, var, index, data):
        e = self._ent(scope)
        self.mem[(scope, var)][index * e : index * e + len(data)] = data

    def read_block(self, addr, n):
        self.passed.append(("r", addr))
        return bytes(n)

    def write_block(self, addr, data):
        self.passed.append(("w", addr))


def test_the_map_is_the_cards():
    # card_run.py's measured anchor: flat 0x10000 lands in channel 0 word 0x100,
    # and each 16 KB granule moves to the next home.
    m = DramMap.of_board(BOARD, 4)
    assert (m.ilv_lg, m.home_lsb, m.nhome_lg) == (14, 32, 2)
    assert m.locate(0x10000) == (0, 0x4000)
    assert [m.locate(c << 14)[0] for c in range(4)] == [0, 1, 2, 3]
    pieces = list(m.runs(0x3F00, 0x200))
    assert [(o, n) for o, _, _, n in pieces] == [(0, 0x100), (0x100, 0x100)]
    assert pieces[0][1] != pieces[1][1]


def test_bytes_land_where_the_map_says_and_merge_unaligned():
    f = FakeModel()
    d = SimDram(f, BOARD, WINDOWS, 1 << 30)
    blob = bytes((k * 7 + 1) & 0xFF for k in range(100))
    d.write_block(WINDOWS[1] + 0x10000 + 13, blob)
    home, inner = d.map.locate(0x10000)
    word = f.mem[(f"TOP.c.u_dram{home}", "mem")][inner : inner + 2 * LINE]
    assert word[13:113] == blob
    assert word[:13] == bytes(13) and word[113:] == bytes(2 * LINE - 113)
    assert (
        d.read_block(WINDOWS[0] + 0x10000 + 13, 100) == blob
    )  # any window, same bytes


def test_a_write_zeroes_exactly_its_lines_rows():
    f = FakeModel()
    d = SimDram(f, BOARD, WINDOWS, 1 << 30)
    d.write_block(WINDOWS[0] + 0x4000 + 2 * LINE, bytes(3 * LINE))  # lines 2..4, home 1
    for h in range(4):
        for b in range(BANKS):
            s = f"TOP.c.u_xache.u_kx.g_home[{h}].u_c.g_bank.g_b[{b}].u_arr.u_ram"
            rows = f.mem[(s, "mem")]
            zero = {
                r for r in range(ROWS) if rows[r * ENT : (r + 1) * ENT] == bytes(ENT)
            }
            want = (
                {(i // BANKS) for i in (2, 3, 4) if i % BANKS == b} if h == 1 else set()
            )
            assert zero == want, (h, b)


def test_outside_a_window_passes_through():
    f = FakeModel()
    d = SimDram(f, BOARD, WINDOWS, 1 << 20)
    d.read_block(0x800000, 64)
    d.write_block(WINDOWS[0] + (1 << 20) - 8, bytes(16))  # runs off the window's end
    assert f.passed == [("r", 0x800000), ("w", WINDOWS[0] + (1 << 20) - 8)]


def test_a_model_without_public_dram_is_refused():
    f = FakeModel()
    f.mem = {k: v for k, v in f.mem.items() if "dram" not in k[0]}
    with pytest.raises(RuntimeError, match="axi_ram"):
        SimDram(f, BOARD, WINDOWS, 1 << 30)
