"""Doorbells, slots and the sig word."""

import conftest


def test_rings_and_counts(probe):
    r = probe("core2", "ring", 4_000_000)
    assert r["rc"] == 0 and not r["fails"], r["output"][-2000:]


def test_the_sig_word_slot_runs_ahead_of_its_count(tmp_path):
    source = conftest.PROGRAMS / "sigslot" / "sigslot.c"
    link = ["-T", str(conftest.PROGRAMS / "xlat" / "xlat.ld"), str(source)]
    a, b = tmp_path / "a.elf", tmp_path / "b.elf"
    conftest.clang("-DROLE_A", *link, "-o", str(a))
    conftest.clang(*link, "-o", str(b))
    out = conftest.core_model("--elf-N", 0, a, "--elf-N", 1, b, "--max-cycles", 400_000)
    m = conftest.measures(out.replace("@ slot.", "@ "))
    assert m["selections"] == 64, out
    assert m["first_matching_word_wrong"] == 64, out
    assert m["next_word_wrong"] == 0, out
