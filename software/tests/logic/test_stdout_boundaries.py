"""Stdout ownership is partitioned by byte positions before UTF-8 decoding."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import Completion, NodeError, NodeQueue
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.card.collection import Collection
from kohakutpu.application.runtime.card.common import Pending
from kohakutpu.application.runtime.card.submission import Submission
from kohakutpu.application.runtime.native import exchange
from kohakutpu.application.runtime.offline import HostMemory

MACHINE = Path(__file__).resolve().parents[3] / "machines" / "multimesh_v9t6.json"


@pytest.fixture(params=[False, True], ids=["queue", "native"])
def output(request, monkeypatch):
    mem = HostMemory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, layout=L.Layout.from_machine(MACHINE))
    q.init()
    written = 0
    native = object.__new__(exchange.NativeExchange)
    native.queue = q
    monkeypatch.setattr(exchange.Completion, "from_bytes", lambda *args: object())

    def append(raw):
        nonlocal written
        mem.write_block(q.base + q.geo["so"] + written, raw)
        written += len(raw)
        mem.write64(q.base + q.layout.so_wr, written)
        if request.param:
            q._list_tags[1] = None
            native.finish(
                SimpleNamespace(acknowledge=lambda: None),
                1,
                (0, written, b"", None, raw, 1, True, None),
            )

    return q, append


def test_completion_boundaries_partition_utf8_bytes(output):
    q, append = output
    append("éX".encode())
    assert q.read_stdout_to(2) == "é"
    assert q.read_stdout_to(3) == "X"


def test_partial_utf8_survives_separate_polls(output):
    q, append = output
    append(b"\xc3")
    assert q.read_stdout() == ""
    append(b"\xa9")
    assert q.read_stdout() == "é"


def test_failed_console_is_partitioned_before_decoding(monkeypatch):
    error = NodeError(Completion(2, 1, 0, 0, 0, 0), "Xé")

    def wait(*args, **kw):
        raise error

    q = NodeQueue(HostMemory(), 0, layout=L.Layout.from_machine(MACHINE))
    q.console_at = 3
    q.ends = {1: 1, 2: 3}
    q.record = q.layout.record
    monkeypatch.setattr(q, "wait", wait)
    card = object.__new__(NodeCard)
    card.queues = {0: q}
    card.q = q
    card.nodes = (0,)
    card.timeout = 1.0
    card.debug = False
    card._order = {0: [1, 2]}
    card._segs, card._stdout = {}, {}
    monkeypatch.setattr(card, "_keep_failed", lambda *args: None)
    segments = []

    def segment(*args):
        text = Submission._segment(card, *args)
        segments.append(text)
        return text

    monkeypatch.setattr(card, "_segment", segment)
    pending = Pending(single=True, tags={0: (2, None)}, plain={0: (None, None)})
    with pytest.raises(NodeError):
        Collection._collect(card, pending, False, None)
    assert segments == ["é"]
    assert card._segs[0][1] == "X"


def test_submission_discard_keeps_next_completion_byte_boundary(output, monkeypatch):
    q, append = output
    append(b"\xc3\xa9\xc3")
    q.read_stdout()
    monkeypatch.setattr(q, "submit", lambda *args, **kw: 9)
    card = object.__new__(NodeCard)
    card.queues = {0: q}
    card._order, card._stdout = {}, {}
    Submission._submit(card, 0, b"package", None, admitted=True)
    append(b"\xa9X")
    assert q.read_stdout_to(4) == "\ufffd"
    assert q.read_stdout_to(5) == "X"
