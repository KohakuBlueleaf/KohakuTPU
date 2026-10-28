"""Literal register words witness the pre-clear firmware snapshot decoder."""

import pickle

import pytest

from kohakuaccel.driver.node.queue import client as Q

CONSOLE = (
    "PKGF de stat de01064301140040 moves 1800000014 mvstat 100000014 xfdata 1\n"
    "PKGF de ctr 0 1 2 3 4 5 6 7 8 9 a b c d e f\n"
)


def test_snapshot_literal_register_layout():
    snap = Q.failure_snapshot(CONSOLE)
    assert snap["raw"] == {
        "stat": 0xDE01064301140040,
        "moves": 0x1800000014,
        "mvstat": 0x100000014,
        "xfdata": 1,
    }
    assert snap["stat"] == {
        "magic": 0xDE01,
        "depth": 64,
        "counter_count": 16,
        "overflow": False,
        "clearing": False,
        "head_blocked": True,
        "enabled": True,
        "issued": 276,
        "queued": 64,
    }
    assert snap["moves"] == {"issued": 24, "done": 20}
    assert snap["mover"] == {
        "room": False,
        "busy": True,
        "fault": 0,
        "fault_name": "none",
        "done": 20,
    }
    assert snap["counters"] == list(range(16))


def test_snapshot_fault_flags_and_free_running_count():
    snap = Q.failure_snapshot(CONSOLE.replace("100000014", "390000abc"))
    assert snap["mover"] == {
        "room": True,
        "busy": True,
        "fault": 9,
        "fault_name": Q.MV.FAULTS[9],
        "done": 0xABC,
    }


def test_missing_engine_and_older_firmware():
    assert Q.failure_snapshot("PKGF stray 0 outstanding 0\n") is None
    snap = Q.failure_snapshot("PKGF de stat 0 moves 0 mvstat 0 xfdata 0\nPKGF de ctr\n")
    assert snap["raw"]["stat"] == 0 and snap["counters"] == []


def test_last_snapshot_is_used_without_merging_counter_blocks():
    assert Q.failure_snapshot(CONSOLE + CONSOLE.replace("1800000014", "1900000015"))[
        "moves"
    ] == {
        "issued": 25,
        "done": 21,
    }


@pytest.mark.parametrize(
    "console",
    [
        CONSOLE.splitlines()[0],
        CONSOLE.replace("moves 1800000014", "moves nope"),
        CONSOLE.replace(" a b c d e f", " a b c d e"),
        CONSOLE.replace("PKGF de ctr ", "PKGF de ctrjunk "),
        CONSOLE.replace("xfdata 1", "xfdata 10000000000000000"),
    ],
)
def test_bad_snapshot_does_not_mask_the_package_error(console):
    c = Q.Completion(7, 0x30, 11, 0, 100, 0xDE01064301140040)
    error = Q.NodeError(c, console)
    assert error.completion is c
    assert error.snapshot is None
    assert error.snapshot_error.startswith("DBG.FAIL.006:")
    assert "TIMEOUT waiting on engine" in str(error)


def test_node_error_snapshot_survives_pickling():
    error = Q.NodeError(Q.Completion(7, 0x30, 11, 0, 100, 0), CONSOLE)
    back = pickle.loads(pickle.dumps(error))
    assert back.snapshot == error.snapshot == Q.failure_snapshot(CONSOLE)
    assert back.snapshot_error is None


def test_dropped_snapshot_mutant_is_caught(monkeypatch):
    monkeypatch.setattr(Q, "failure_snapshot", lambda text: None)
    with pytest.raises((AssertionError, TypeError)):
        test_snapshot_literal_register_layout()
