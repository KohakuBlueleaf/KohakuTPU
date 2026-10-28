"""A never-completing take is bounded, including a stopped model clock."""

from itertools import count
from types import SimpleNamespace

import pytest
from runners.target.l0 import run_once


class Queue:
    def __init__(self, complete=None):
        self.polls = self.idles = 0
        self.complete = complete
        self.console = "done\n"
        self.idle = self.advance

    def submit(self, raw, bindings, **kw):
        return 1

    def _start(self):
        return 0

    def _waited(self, start, what):
        return 0

    def take(self, tag):
        self.polls += 1
        # A guard makes undoing the bound fail promptly, not hang the suite.
        assert self.polls <= 32_001, "unbounded take loop"
        return SimpleNamespace(ok=True) if self.polls == self.complete else None

    def advance(self):
        self.idles += 1

    def read_stdout(self):
        return self.console


@pytest.mark.parametrize(
    "cycles,budget", [(1, 1000), (2_000_000, 16000), (4_000_000, 32000), (0, 16000)]
)
def test_never_completing_take_is_bounded(cycles, budget):
    q = Queue()
    with pytest.raises(TimeoutError, match=f"{budget} polls"):
        run_once(q, b"", cycles)
    assert q.polls == q.idles == budget


def test_removing_the_poll_bound_fails_the_witness(monkeypatch):
    monkeypatch.setitem(run_once.__globals__, "range", lambda _: count())
    with pytest.raises(AssertionError, match="unbounded take loop"):
        test_never_completing_take_is_bounded(1, 1000)


def test_completion_at_the_budget_is_accepted():
    q = Queue(complete=1000)
    c, at, text = run_once(q, b"", 1)
    assert c.ok and at == text == "done\n"
    assert q.polls == 1000 and q.idles == 999
