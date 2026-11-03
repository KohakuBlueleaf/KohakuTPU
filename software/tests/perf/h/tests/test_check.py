"""The round h checks, offline: the tag layout against hand-computed words, every
checker mutation control failing, structural verdicts apart from precision, the
token ledger against a body's rings, and C4's frontier inspection."""

import pathlib

import numpy as np

from h import check, frontier
from h import ledger as L
from h.gen.body import Body
from kohakutpu.application.tools.l1 import POISON


def test_a_tag_word_is_its_four_fields_in_positive_normal_f16_lanes():
    # offset 0x12345: low 0x345, high 0x48; epoch 5; producer 1, region 2.
    assert check.tag(5, 1, 2, 0x12345) == 0x1042_0C05_0848_0745
    assert check.decode(0x1042_0C05_0848_0745) == dict(
        epoch=5, producer=1, region=2, offset=0x12345
    )
    lanes = check.tags(1023, 15, 63, np.arange(1 << 20)).view(np.float16)
    assert np.isfinite(lanes).all() and (lanes > 0).all()
    assert check.decode(check.POISON_WORD) is None
    assert check.tag(0, 3, 1, 7) + check.epoch_bits(9) == check.tag(9, 3, 1, 7)


def test_every_checker_mutation_fails_and_rerounding_stays_unresolved():
    got = check.sensitivity()
    assert check.insensitive(got) == []
    assert got["captured"]["structural"] == "PASS"
    assert got["numeric_rerounded"] == dict(
        structural="PASS", precision="UNRESOLVED", reasons=[]
    )
    failed = [k for k, v in got.items() if v["structural"] == "FAIL"]
    assert len(failed) == len(got) - 2


def test_exact_counts_poison_left_and_poison_wanted_words():
    want = check.tags(1, 0, 0, np.arange(4))
    want[3] = check.POISON_WORD
    got = want.copy()
    got[1] = check.POISON_WORD
    out = check.exact(got.tobytes(), want.tobytes())
    assert (out["mismatched"], out["poison_left"], out["poison_equal"]) == (1, 1, 1)
    assert out["first"][0]["want_tag"] == dict(epoch=1, producer=0, region=0, offset=1)


def test_a_diagnostic_mismatch_manifests_but_a_redzone_hit_still_fails():
    bad = {"y": dict(mismatched=1)}
    assert check.verdict(bad, {}, {}, [], True)["structural"] == "MANIFEST"
    assert check.verdict({"y": {}}, {}, {}, [], True)["structural"] == "INCONCLUSIVE"
    assert check.verdict(bad, {"y": [3]}, {}, [], True)["structural"] == "FAIL"
    assert check.verdict({}, {}, {0: "timeout"}, [], False)["structural"] == "FAIL"


def test_a_numerical_output_left_poison_or_nonfinite_is_structural():
    ref = np.ones(8, np.float16)
    got = ref.copy()
    got.view(np.uint8)[:2] = POISON
    out = check.numeric(got, ref)
    assert out["poison_left"] == 1 and out["nonfinite"] == 0
    assert check.verdict({"v": out}, {}, {}, [], False)["structural"] == "FAIL"


def test_a_body_ledger_counts_packets_by_source_and_the_slots_it_reads_back():
    b = Body(ledger=True)
    b.ring(0, 2, 3, amount=5)
    b.ring(1, 2, 3, times=4)
    b.ring(2, 0, 1)
    text = b.fn("f", "unused: u64[1] @dram")
    ledger = b.ledger_of()
    assert ledger.slots == ((0, 1), (2, 3))
    assert "slots: u64[2] @dram" in text and "ctrl il_select <- 3" in text
    want = L.expected(ledger)
    assert want[2]["bells_from0"] == 1 and want[2]["bells_from1"] == 4
    assert want[1]["bells_sent"] == 4 and want[2]["bells_sent"] == 1
    assert L.check(ledger, want, L.settled(ledger)) == []
    left = L.settled(ledger)
    left[1] |= np.uint64(5)
    assert L.check(ledger, want, left) == ["sysnode 2 slot 3: read slot 3, left 5"]


ONE = """fn f.l1(x: u64[1] @dram)
    node 0
        send vc[0]
            run a(x)
        send vc[1]
            run b(x)
        k = mark vc[1]
        wait vc[1] upto=k
        ring mesh=1 slot=0
        wait vc[0]
        ring mesh=1 slot=1
    node 1
        wait slot 0 consume n=1
"""


def test_the_frontier_names_units_no_wait_covers_at_each_ring():
    got = frontier.rings(ONE, "f")
    assert [(r["sysnode"], r["uncovered"]) for r in got] == [(0, ["vc[0]"]), (0, [])]
    assert got[0]["waits"] == ["wait vc[1] upto=k"]
    assert got[1]["waits"] == ["wait vc[0]"]


def test_the_frontier_reads_the_current_compilers_witness():
    witness = pathlib.Path(__file__).parents[1] / "gen" / "witness"
    path = witness / "release_frontier.ktpu"
    l1_text, name = frontier.emitted(path, "multimesh_v9t6", 4)
    got = frontier.rings(l1_text, name)
    assert [r["sysnode"] for r in got] == [0]
