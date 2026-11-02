"""The kernel's unit accounting over a hardware double of the dispatch engine: a peer's
retirement of a unit this node sent to must neither hold a descriptor stream's end
nor the next ticket, and a kernel copy must not inherit a window on either axis."""

import native
import pytest

SOURCES = ["os/kernel/units.c"]
MUTANTS = {
    "unsigned_quiet": (
        "(int64_t)(kunits[u].sent - retired[u]) > 0",
        "kunits[u].sent != retired[u]",
    ),
    "no_reconcile": ("kunits[u].sent = retired[u];", ""),
    "one_axis": ("hw_wr(MV(0x28), sel | 2);", ""),
}


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    return native.on_double(tmp_path_factory.mktemp("units"), "kos_units", SOURCES)


def after_peer(exe):
    return native.run(exe, ["s 2 1", "d", "d", "d", "d", "d", "d", "w"])


def test_a_stream_of_words_retires_them(exe):
    assert native.run(exe, ["s 2 0", "d", "w"]) == ["0 2 2", "3", "3 3"]


def test_a_peer_retirement_ends_its_stream_and_counts_as_sent(exe):
    assert after_peer(exe)[0] == "0 3 3"


def test_tickets_after_a_peer_wait_for_their_own_words(exe):
    lines = after_peer(exe)
    assert lines[1:7] == ["4", "5", "6", "7", "8", "9"]
    assert lines[7] == "9 9"


def test_a_copy_writes_both_axes_window_on_both_sides(exe):
    assert native.run(exe, ["m"]) == ["0 1 1 1 1"]


def test_a_stream_with_no_peer_still_waits_for_what_it_sent(tmp_path):
    late = native.on_double(tmp_path, "kos_units", SOURCES, MUTANTS["no_reconcile"])
    assert native.run(late, ["s 2 0", "w"]) == ["0 2 2", "2 2"]


@pytest.mark.parametrize("name", MUTANTS)
def test_each_mutant_is_told(tmp_path, exe, name):
    changed = native.on_double(tmp_path, "kos_units", SOURCES, MUTANTS[name])
    script = (
        ["m"] if name == "one_axis" else ["s 2 1", "d", "d", "d", "d", "d", "d", "w"]
    )
    assert native.run(changed, script) != native.run(exe, script)
