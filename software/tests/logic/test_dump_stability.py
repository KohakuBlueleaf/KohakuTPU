"""Automatic dump stability requires post-quiesce evidence for its tag."""

import pytest

from kohakuaccel.driver.node.queue.client import Completion, NodeError, NodeQueue
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.card.collection import Collection
from kohakutpu.application.runtime.session import DebugSession


@pytest.mark.parametrize(
    "report,console,stable",
    [
        ({"packages": [{"tag": 7, "fail": {"drained": True}}]}, "", True),
        ({"packages": []}, "", False),
        ({"packages": [{"tag": 7, "fail": None}]}, "", False),
        (None, "PKGF stray 0 outstanding 0", False),
    ],
)
def test_session_requires_matching_fail_evidence(report, console, stable):
    error = NodeError(Completion(7, 1, 0, 0, 0, 0), console)
    error.debug = report
    card = object.__new__(NodeCard)
    card.clock = lambda: 0.0
    card.debug = False
    session = DebugSession(card, seconds=10, wait=2)
    session.t0, session._end = 0.0, 10.0

    def fail():
        raise error

    assert session.attempt(fail)["drained"] is stable


def test_rerun_restarts_before_using_unknown_failure_state(monkeypatch):
    events = []
    report = {"packages": []}

    def run(*args, **kw):
        assert events == ["restart"]
        events.append("run")

    card = object.__new__(NodeCard)
    queue = object.__new__(NodeQueue)
    monkeypatch.setattr(queue, "run", run)
    card.queues = {0: queue}
    card.timeout = 1.0
    monkeypatch.setattr(card, "restart", lambda: events.append("restart"))
    monkeypatch.setattr(card, "debug_report", lambda node: report)
    Collection._debug_rerun(card, 0, b"package", None, report)
    assert events == ["restart", "run"]
