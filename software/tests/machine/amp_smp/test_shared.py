"""What several nodes see of one memory: ordering litmus, atomics, and memory
that is one node's alone."""

import pytest


@pytest.mark.parametrize("cmd", ["sc", "amo"])
def test_shared_memory(probe, cmd):
    r = probe("core4s", cmd, 8_000_000)
    assert r["rc"] == 0 and not r["fails"], r["output"][-2000:]


def test_a_wrong_board_is_told(probe):
    r = probe("core2", "amo", 100_000)
    assert r["rc"] != 0 or "shared" in r["output"] or r["fails"]
