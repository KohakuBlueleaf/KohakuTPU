"""A host-joined sequence has different duration aggregates, not one makespan."""

from types import SimpleNamespace

import pytest
import retirement

from kohakutpu.application.runtime import device as D


def test_part_maxima_are_not_the_maximum_of_node_sums():
    run = SimpleNamespace(name="sequence", kind="graph", wall=1.0, info={"seq": 3})
    entry = dict(
        run=run,
        backend="kaos",
        phases={"makespan_seconds": 1.0},
        nodes={
            "0": {"cycles": 101, "part_results": [{"cycles": 100}, {"cycles": 1}]},
            "1": {"cycles": 101, "part_results": [{"cycles": 1}, {"cycles": 100}]},
        },
    )
    row = retirement.card_rows([entry])[0]
    assert row["max_node_cycles"] == 101
    assert row["sum_part_max_node_cycles"] == 200
    assert row["makespan_seconds"] == 1.0
    assert row["cycle_aggregation"] == "maximum_node_sum_not_sequence_makespan"
    assert (
        row["cycle_scope"]
        == "sum_of_node_local_part_durations_excludes_host_transitions"
    )


def test_observer_neither_reads_nor_tears_down_a_dead_transport(tmp_path, monkeypatch):
    monkeypatch.delenv("PERF_CLOCKS", raising=False)

    def init(self, *args, **kwargs):
        self.closed = False
        self._failure = OSError("C2H timeout")
        self._uncertain = []

    def forbidden(*args, **kwargs):
        raise AssertionError("observer accessed or closed a dead card")

    monkeypatch.setattr(D.Device, "__init__", init)
    monkeypatch.setattr(D.Device, "close", forbidden)
    for name in ("_collect_one", "read"):
        monkeypatch.setattr(D.Device, name, getattr(D.Device, name))
    finish = retirement.observe(tmp_path, "firmware")
    dev = D.Device("offline")
    monkeypatch.setattr(
        dev, "_perf_ring", SimpleNamespace(rows=forbidden), raising=False
    )
    monkeypatch.setattr(
        dev, "_perf_package_transport_failed", lambda: True, raising=False
    )
    with pytest.raises(RuntimeError, match="teardown skipped"):
        dev.close()
    with pytest.raises(RuntimeError, match="measurement read skipped"):
        finish()
    assert not dev.closed  # no false shutdown/ownership-reclamation claim
