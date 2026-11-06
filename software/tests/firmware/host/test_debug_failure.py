"""FAIL records describe first-failure position and post-quiesce stability."""

import native
import pytest


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    exe = native.on_double(
        tmp_path_factory.mktemp("debug"),
        "debug_failure",
        [],
        app="firmware/package/debug.c",
    )
    return native.run(exe, [])


def test_secondary_fault_does_not_replace_first_issue_count(records):
    assert records[:2] == ["37", "9"]
    assert records[3] == "0"


def test_busy_mover_is_not_drained(records):
    assert records[2] == "0"
