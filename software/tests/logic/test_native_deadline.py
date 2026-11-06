"""Native publication uses the ordinary timeout or the session's remaining wait."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.native import exchange
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.application.runtime.session import DebugSession, SessionBudget

MACHINE = Path(__file__).resolve().parents[3] / "machines" / "multimesh_v9t6.json"


def test_native_submit_deadline_precedes_publication(monkeypatch):
    now, waits, published = [0.0], [], []
    q = NodeQueue(
        HostMemory(), 0, sq_n=2, cq_n=4, layout=L.Layout.from_machine(MACHINE)
    )
    q.init()
    native = object.__new__(exchange.NativeExchange)
    native.queue = q
    native.transport = SimpleNamespace(_c2h=1, _h2c=2, fd_generation=0)
    native.handles, native.generation = (1, 2), 0
    native.base, native.geometry = 0, (2, 4, 0, 0, 0)

    def run(base, geometry, tail, head, stdout, entry, timeout):
        waits.append(timeout)
        return head + 1, stdout, b"", None, b"", 1, True, None

    monkeypatch.setattr(native, "endpoint", SimpleNamespace(run=run), raising=False)
    q.native_exchange = native
    monkeypatch.setattr(exchange.Completion, "from_bytes", lambda *args: object())
    execution = SimpleNamespace(
        descriptor=SimpleNamespace(address=0x1000), count=1, generation=0, inflight=0
    )

    def acknowledge():
        execution.inflight -= 1

    execution.acknowledge = acknowledge
    card = object.__new__(NodeCard)
    card.queues = {0: q}
    card.timeout = 1.0
    card.clock = lambda: now[0]
    card.sys_hz = 1_000_000
    card.boots = {0: (None, SimpleNamespace(timeout=100))}
    monkeypatch.setattr(card, "load", lambda **kw: None)

    def publishing():
        published.append(q._tag)

    native._submit(execution, publishing)
    assert waits == [1.0]
    with DebugSession(card, seconds=10, wait=2):
        now[0] = 7.0
        native._submit(execution, publishing)
        assert waits == [1.0, 3.0]
        now[0] = 10.0
        before = q._tag
        with pytest.raises(SessionBudget):
            native._submit(execution, publishing)
        assert q._tag == before
        assert published == [1, 2]
        assert not execution.inflight
        assert not q._list_tags
    assert "_submit" not in native.__dict__
    assert q.timeout == 1.0
