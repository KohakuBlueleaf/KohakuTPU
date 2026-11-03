"""ISSUES 79's stamp and fill pieces, offline: a stamp is the exact f16 multiply
it stands for, the classifier names each synthetic corruption's cut distinctly
from raw words, every piece's reads hold a one-beat read exactly where the
table says, and the unstamped whole fill is P12's put."""

import functools

import numpy as np
import pytest

from h import check, gen, stamp
from h.gen import c2
from h.gen.p12 import put_image
from h.tests.test_gen import BOARDS, SYSNODES, placed
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card.common import MACHINES
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.tools import l1
from kohakutpu.language.text import module as M
from kohakutpu.stack import kernel

NAMES = [
    f"c2_i79_{'_'.join(map(str, p))}_{kind}"
    for p in c2.PIECES
    for kind in ("fresh", "stamp")
]
EPOCH = 1029
ROWS, WIDTH, READERS, TILE = 4, 64, 2, 64


def test_a_stamp_is_the_f16_multiply_by_its_writers_power_of_two():
    words = check.tags(EPOCH, 3, 7, np.arange(0, 1 << 20, 997))
    for w, delta in enumerate(stamp.DELTAS):
        scaled = words.view(np.float16).astype(np.float64) * 2.0**delta
        want = scaled.astype(np.float16).view(np.uint64)
        assert np.isfinite(scaled).all()
        assert ((words + np.uint64(stamp.stamp(w))) == want).all()
        got = stamp.decode(int(want[5]))
        assert got == dict(
            writer=w, producer=3, region=7, offset=7 << 20 | 5 * 997, epoch=EPOCH % 1024
        )


def gathered(stamped: bool):
    """The small gather's x (epoch EPOCH, injective offsets) and wanted y."""
    words = ROWS * WIDTH // 8
    x = np.concatenate([check.tags(EPOCH, s, 0, np.arange(words)) for s in SYSNODES])
    x = stamp.stamped(x, len(SYSNODES)) if stamped else x
    y = x.reshape(len(SYSNODES), ROWS, WIDTH // 8).transpose(1, 0, 2).reshape(-1)
    return x, y


def test_each_corruption_names_its_cut_from_the_raw_words():
    g = stamp.Gather(SYSNODES, ROWS, WIDTH, READERS, TILE, True)
    x, want = gathered(True)
    got = want.copy()
    per = ROWS * WIDTH // 8
    # y word 9 is writer 1's offset 1: producer 3's offset 6 under writer 1's stamp.
    got[9] = x[3 * per + 6] - np.uint64(stamp.stamp(3)) + np.uint64(stamp.stamp(1))
    # y word 18 is writer 2's offset 2: producer 0's offset 4 with its own stamp.
    got[18] = x[4]
    # y word 32 is writer 0's offset 8: its own producer's offset 9, its stamp.
    got[32] = x[9]
    # y word 40: lane 0 under writer 2's stamp, the rest under writer 1's.
    got[40] = want[40] + np.uint64((stamp.DELTAS[2] - stamp.DELTAS[1]) << 10)
    binds = {"x": 0x100000, "y": 0x200000, "v": 0x300000}
    first = check.exact(got.tobytes(), want.tobytes())
    rec = g({"y": first | {"reread": first}}, binds, EPOCH)["y"]
    words = {w["at"]: w for w in rec["first"]["words"]}
    assert rec["reread"]["kinds"] == rec["first"]["kinds"]
    assert {at: w["kind"] for at, w in words.items()} == {
        9: "before_victim_transform",
        18: "after_donor_transform",
        32: "before_victim_transform",
        40: "invalid_or_mixed",
    }
    a, b, c = words[9], words[18], words[32]
    assert (a["got"]["producer"], a["got"]["offset"], a["got"]["writer"]) == (3, 6, 1)
    assert (a["writer"], a["offset"], a["producer_ok"], a["epoch_ok"]) == (
        1,
        1,
        False,
        True,
    )
    assert a["origin_x"] == binds["x"] + 3 * ROWS * WIDTH + 8 * 6
    assert (b["got"]["writer"], b["got"]["producer"], b["writer"]) == (0, 0, 2)
    assert b["origin_y"] == binds["y"] + 4 * 8
    assert (c["producer_ok"], c["offset_ok"], c["got"]["offset"]) == (True, False, 9)
    assert words[40]["got"] is None and words[40]["raw"][0] == f"{int(got[40]):#018x}"


def test_a_consumers_v_word_maps_to_its_y_word_and_a_control_cannot_cut():
    g = stamp.Gather(SYSNODES, ROWS, WIDTH, READERS, TILE, False)
    x, y = gathered(False)
    share = ROWS // READERS * len(SYSNODES) * WIDTH // 8
    want = np.concatenate(
        [y[(r + 1) * share - TILE // 8 : (r + 1) * share] for r in range(2)]
    )
    got = want.copy()
    got[8] = x[0]
    out = check.exact(got.tobytes(), want.tobytes())
    rec = g({"v": out}, {"x": 0, "y": 0, "v": 0}, EPOCH)["v"]["first"]
    (word,) = rec["words"]
    assert word["kind"] == "unstamped" and word["y_address"] == 8 * (2 * share - 8)
    assert (word["writer"], word["producer_ok"], word["offset_ok"]) == (3, False, False)


def test_one_beat_reads_are_where_the_pieces_table_says():
    def single(pieces):
        return [(a, n) for piece in c2.reads(pieces) for a, n in piece if n == 1]

    assert c2.reads((256,))[0][-2:] == [(7680, 8), (8128, 1)]
    assert single((256,)) == single((255, 1)) == [(8128, 1)]
    assert single((254, 2)) == [(8128, 1)]
    assert single((128, 128)) == single((253, 3)) == single((252, 4)) == []
    assert c2.reads((253, 3))[1] == [(8064, 2)]
    for pieces in c2.PIECES:
        assert sum(pieces) == c2.TILE_WORDS


@functools.cache
def module(board: str):
    mod = gen.load("c2", SYSNODES, board)
    return mod, M.read(mod.source, "<c2>")


@pytest.mark.parametrize("board", BOARDS)
@pytest.mark.parametrize("name", NAMES)
def test_each_i79_fn_compiles_and_the_checker_passes_it(name, board):
    mod, text = module(board)
    assert name in mod.specs and isinstance(mod.specs[name].provenance, stamp.Gather)
    machine = machine_of(board, nodes=len(SYSNODES))
    layout = l1.bufs(text, name, (MACHINES / f"{board}.json").read_text("utf-8"))
    progs = kernel.compile(text, name, machine, level="l1").programs(
        placed(layout, machine)
    )
    assert B.findings(progs, "refuse") == []
    fn = mod.source[mod.source.index(f"fn {name}.l1(") :]
    for q in SYSNODES:
        block = fn[fn.index(f"    node {q}") :]
        put = block[block.index("run i79_") :].split("(")[0]
        assert put.endswith(f"_w{q}") == name.endswith("stamp"), (q, put)


def test_the_unstamped_whole_fill_is_p12s_put():
    name, text = c2.i79_image((256,), 32768, None)
    p12_name, p12_text = put_image(8192, 1, 32768)
    got = text.replace(name, p12_name).replace("src0", "src")
    assert got.split() == p12_text.split()
