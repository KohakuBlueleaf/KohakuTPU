"""Recorder-owned queue hooks must not survive a failed trace save."""

import pytest
import record
from test_package_check import install

from kohakuaccel.driver.node.queue.client import NodeQueue


def test_save_failure_restores_queue_hooks_and_closes_the_launch_file(
    tmp_path, monkeypatch
):
    original = NodeQueue.submit
    trace = record.Trace(tmp_path)
    install(trace, monkeypatch)
    assert NodeQueue.submit is not original  # base installs no package observer

    def full_disk():
        raise OSError("launch trace disk full")

    monkeypatch.setattr(trace, "persist_launches", full_disk)
    with pytest.raises(OSError, match="disk full"):
        trace.save({})
    assert NodeQueue.submit is original
    assert trace.launches.closed
