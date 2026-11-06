"""All session wait phases share one budget without reserving idle time."""

from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue.client import Completion, NodeQueue
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.session import DebugSession


class Queue(NodeQueue):
    def __init__(self, now: list[float]):
        self.alarm = 0.0
        self.timeout = 1.0
        self.now = now
        self.clock = lambda: now[0]
        self.allowances: list[float] = []

    def _start(self) -> tuple[float, float]:
        return self.clock(), 0.0

    def wait(
        self, tag: int, timeout: float | None = None, check: bool = True, service=None
    ) -> Completion:
        timeout = self.timeout if timeout is None else timeout
        start = self._start()
        self.allowances.append(timeout - self._waited(start, "phase"))
        self.now[0] += tag
        if self._waited(start, "phase") > timeout:
            raise TimeoutError("phase exceeded shared deadline")
        return Completion(tag, 0, 0, 0, 0, 0)

    def wait_ready(self, timeout: float = 120.0) -> dict:
        self.wait(0, timeout)
        return {}


def card_at(now, monkeypatch):
    card = object.__new__(NodeCard)
    card.queues = {0: Queue(now)}
    card.timeout = 1.0
    card.clock = lambda: now[0]
    card.sys_hz = 1_000_000
    card.boots = {0: (None, SimpleNamespace(timeout=100))}
    monkeypatch.setattr(card, "load", lambda **kw: None)
    return card


def test_immediate_attempt_needs_no_reserved_wait_intervals(monkeypatch):
    now = [0.0]
    card = card_at(now, monkeypatch)
    with DebugSession(card, seconds=10.0, wait=2.0) as session:
        now[0] = 9.0
        assert session.attempt(lambda: "immediate")["result"] == "immediate"


def test_later_waits_get_only_the_original_deadlines_remaining_time(monkeypatch):
    now = [0.0]
    card = card_at(now, monkeypatch)
    q = card.queues[0]
    assert isinstance(q, Queue)
    with DebugSession(card, seconds=10.0, wait=2.0) as session:

        def phases():
            q.wait(7, timeout=card.timeout)
            q.wait(2, timeout=card.timeout)

        session.attempt(phases)
    assert q.allowances == [10.0, 3.0]
    assert card.timeout == 1.0
    assert card.boots[0][1].timeout == 100
    assert not {"wait", "wait_ready", "_start"} & q.__dict__.keys()


def test_quiesce_cannot_renew_execution_budget(monkeypatch):
    now = [0.0]
    card = card_at(now, monkeypatch)
    q = card.queues[0]
    assert isinstance(q, Queue)
    with DebugSession(card, seconds=10.0, wait=2.0) as session:

        def phases():
            q.wait(7, timeout=card.timeout)
            q.wait(4, timeout=card.timeout)

        with pytest.raises(TimeoutError):
            session.attempt(phases)
