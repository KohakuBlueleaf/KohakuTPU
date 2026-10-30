"""The fill completion queue on a Verilated card (ISA.VC.193): a program the
checker refuses, run past it with the check mutated off, against numpy.

    KTPU_CARD1=build/v9t6/vlt_card_v9t6_1n KTPU_BOARD=multimesh_v9t6 pytest ...
"""

import os

import numpy as np
import pytest

from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.application.tools.l1 import run
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import rs

CARD1 = os.environ.get("KTPU_CARD1")
BOARD = os.environ.get("KTPU_BOARD", "multimesh_v9t6")
NO_CHECK = "verify.vector/no-fill-track"

HEADED = """target ktpu.v9

image over(x, y, out)
    desc a0 = x walk=(32, 256)
    desc a1 = y walk=(32, 1)
    desc a2 = out walk=(32, 8)
    ainc a1 += 32
    vfill a0 -> l1[0]
    for i in 0..{behind}
        vfill a1 -> l1[256 + i] rel
    vsync drain on=fill
    vdrain a2 <- l1[248]
    halt

fn over.l1(x: f16[4096] @dram, y: f16[512] @dram, out: f16[128] @dram)
    send vc[0]
        run over(x, y, out)
    barrier
"""

EQUAL = """target ktpu.v9

image same(x, out)
    desc a0 = x walk=(32, 128)
    desc a2 = out walk=(32, 8)
    ainc a0 += 4K
    for i in 0..{fills}
        vfill a0 -> l1[0] rel
    vsync drain on=fill
    vdrain a2 <- l1[120]
    halt

fn same.l1(x: f16[{words}] @dram, out: f16[128] @dram)
    send vc[0]
        run same(x, out)
    barrier
"""


@pytest.fixture(scope="module")
def card():
    if not CARD1:
        pytest.skip("KTPU_CARD1 names no card model")
    c = VerilatorCard(CARD1, board=BOARD, check="off")
    yield c
    c.close()


def f16(seed, n):
    return np.random.default_rng(seed).standard_normal(n).astype(np.float16)


def stale_lanes(card, tmp_path, behind):
    x, y = f16(behind, 4096), f16(behind + 1, 512)
    path = tmp_path / f"over{behind}.ktpu"
    path.write_text(HEADED.format(behind=behind))
    out = run(card, str(path), {"x": x, "y": y}, reps=1)["out"]["out"]
    return np.count_nonzero(out != x[3968:])


def test_a_queue_full_of_fills_retires_every_one_in_turn(card, tmp_path):
    assert stale_lanes(card, tmp_path, 15) == 0


def test_a_shadowed_word_count_retires_its_fill_early(card, tmp_path):
    if NO_CHECK not in rs.mutants():
        pytest.skip("this extension carries no mutants")
    with pytest.raises(TextError, match="the completion queue holds 16"):
        stale_lanes(card, tmp_path, 16)
    with rs.mutant(NO_CHECK):
        assert stale_lanes(card, tmp_path, 16) == 128


def test_an_equal_word_count_shadows_nothing(card, tmp_path):
    fills = 31
    words = fills * 2048
    x = f16(fills, words)
    path = tmp_path / "same.ktpu"
    path.write_text(EQUAL.format(fills=fills, words=words))
    out = run(card, str(path), {"x": x}, reps=1)["out"]["out"]
    assert np.array_equal(out, x[words - 128 :])
