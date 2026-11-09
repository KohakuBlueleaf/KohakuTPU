"""The kernel's unit accounting over a hardware double of the dispatch engine: a peer's
retirement of a unit this node sent to must neither hold a descriptor stream's end
nor the next ticket, and a kernel copy must not inherit a window on either axis.
Boot crosses the resident cache barrier before allocating pending requests."""

import native
import pytest

SOURCES = ["os/kernel/units.c", "os/kernel/bootstrap_units.c"]
#: Double's cycles a stream spends outside its stamps: entry checks and the return.
SPAN_SLACK = 12
COUNTERS = (
    "streams",
    "entries",
    "open",
    "load",
    "room",
    "tail",
    "span",
    "moves",
    "move",
)
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


def test_a_paused_epoch_keeps_incoming_completions_and_does_not_drain_its_prefix(exe):
    assert native.run(exe, ["e"]) == ["epoch retired local and incoming work"]


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


def counters(exe, script):
    values = map(int, native.run(exe, [*script, "c"])[-1].split())
    return dict(zip(COUNTERS, values, strict=True))


def test_a_short_stream_never_waits_for_queue_room(exe):
    c = counters(exe, ["s 2 0"])
    assert c["streams"] == 1 and c["entries"] == 13
    assert c["room"] == 0 and c["load"] > 0 and c["tail"] > 0


def test_a_stream_past_the_queue_counts_its_waits_for_room(exe):
    assert counters(exe, ["s 60 0"])["room"] > 0


def test_a_stream_counters_add_up_to_its_span(exe):
    c = counters(exe, ["s 2 0", "s 60 0", "s 3 1"])
    parts = c["open"] + c["load"] + c["room"] + c["tail"]
    assert c["streams"] == 3
    assert parts <= c["span"] <= parts + 3 * SPAN_SLACK


def test_a_copy_is_counted(exe):
    c = counters(exe, ["m", "m"])
    assert c["moves"] == 2 and c["move"] > 0


@pytest.mark.parametrize(
    ("change", "check"),
    [
        (
            (
                "kstat.cyc_de_room += hw_cycles() - t0;",
                "kstat.cyc_de_room += (hw_cycles() - t0) * 0;",
            ),
            test_a_stream_past_the_queue_counts_its_waits_for_room,
        ),
        (
            (
                "kstat.cyc_de_load += t_tail - t_load - (kstat.cyc_de_room - room0);",
                "kstat.cyc_de_load += t_tail - t_load + (room0 & 0);",
            ),
            test_a_stream_counters_add_up_to_its_span,
        ),
        (
            (
                "kstat.cyc_de_open += t_load - t_open;",
                "kstat.cyc_de_open += (t_load - t_open) * 0;",
            ),
            test_a_stream_counters_add_up_to_its_span,
        ),
    ],
)
def test_each_lost_term_is_told(tmp_path, change, check):
    changed = native.on_double(tmp_path, "kos_units", SOURCES, change)
    with pytest.raises(AssertionError):
        check(changed)


@pytest.mark.parametrize("name", MUTANTS)
def test_each_mutant_is_told(tmp_path, exe, name):
    changed = native.on_double(tmp_path, "kos_units", SOURCES, MUTANTS[name])
    script = (
        ["m"] if name == "one_axis" else ["s 2 1", "d", "d", "d", "d", "d", "d", "w"]
    )
    assert native.run(changed, script) != native.run(exe, script)
