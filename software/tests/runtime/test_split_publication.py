"""Split submissions publish every participating queue before collection."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import NodeCard


@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.parametrize("path", ["wire", "recorded", "prepared", "replay", "list"])
def test_split_submission_publishes_every_queue_before_collection(
    path, immediate, monkeypatch
):
    monkeypatch.setattr(B, "admit", lambda *args, **kwargs: None)
    card = object.__new__(NodeCard)
    card.nodes = tuple(range(4))
    card.machine_json = "{}"
    card._eager_batch = None
    card.admission_cached = False
    card.log = None
    card.residents = {}
    card._order, card._stdout = {}, {}
    card._publication_epoch = 0
    card.runs = 0
    card.timeout = 1.0
    order = []
    published = set()
    uploaded = set()
    bound = set()

    def prepare(node):
        uploaded.add(node)
        order.append(("upload", node))
        return SimpleNamespace(close=lambda: None)

    def flush(node):
        assert node in bound
        if node not in published:
            order.append(("publish", node))
            published.add(node)

    def submit(node, *args, publishing=None, **kwargs):
        if path != "list":
            assert uploaded == set(card.nodes)
        bound.add(node)
        order.append(("stage", node))
        if publishing is not None:
            publishing()
        if immediate:
            flush(node)
        return node + 1

    def wait(node, *args, **kwargs):
        assert published == set(card.nodes)
        order.append(("collect", node))
        return SimpleNamespace(cycles=0)

    queues = {
        node: SimpleNamespace(
            prepare=lambda data, node=node: prepare(node),
            flush=lambda node=node: flush(node),
            submit=lambda *args, node=node, **kwargs: submit(node, *args, **kwargs),
            submit_list=lambda *args, node=node, **kwargs: submit(
                node, *args, **kwargs
            ),
            wait=lambda *args, node=node, **kwargs: wait(node),
            read_stdout=lambda: "",
            discard_stdout=lambda: None,
            record=None,
        )
        for node in card.nodes
    }
    monkeypatch.setattr(card, "queues", queues, raising=False)
    monkeypatch.setattr(card, "_segment", lambda *args: "", raising=False)
    raw = {node: (b"package", ()) for node in card.nodes}
    if path == "wire":
        recipe = SimpleNamespace(plain=lambda *args: b"plain")
        card.submit_wire(raw, {node: (recipe, (), ()) for node in card.nodes})
    elif path == "recorded":
        card.submit_recorded(raw)
    elif path == "prepared":
        card.submit_prepared({node: (b"package", None, ()) for node in card.nodes})
    elif path == "list":
        card.submit_execution({node: (object(), ()) for node in card.nodes})
    else:
        card.replay([("run", raw)])
    assert published == set(card.nodes)
    if path != "list":
        assert order[:4] == [("upload", node) for node in card.nodes]
    assert [node for event, node in order if event == "stage"] == list(card.nodes)
    assert [node for event, node in order if event == "publish"] == list(card.nodes)
    for node in card.nodes:
        assert order.index(("stage", node)) < order.index(("publish", node))
    for index, (event, _) in enumerate(order):
        if event == "collect":
            assert all(order.index(("publish", node)) < index for node in card.nodes)


def test_a_list_on_a_record_queue_segments_its_console_bytes(monkeypatch):
    card = object.__new__(NodeCard)
    card.nodes = (0,)
    card._order, card._stdout, card._segs = {}, {}, {}
    card._publication_epoch = 0
    card.runs = 0
    card.timeout = 1.0
    card.residents = {}
    text = b"list run text\n"
    queue = SimpleNamespace(
        record=object(),
        ends={},
        console_at=0,
        flush=lambda: None,
        read_stdout=lambda: b"",
        wait=lambda *args, **kwargs: SimpleNamespace(cycles=5),
    )

    def submit_list(*args, publishing=None, **kwargs):
        queue.ends[1] = len(text)
        return 1

    def read_to(end):
        queue.console_at = end
        return text[:end]

    queue.submit_list = submit_list
    queue.read_stdout_bytes_to = read_to
    monkeypatch.setattr(card, "queues", {0: queue}, raising=False)
    monkeypatch.setattr(
        card,
        "_result",
        lambda text, done, _: {"cycles": done.cycles, "text": text},
        raising=False,
    )
    pending = card.submit_execution({0: (object(), ())})
    assert card.collect(pending)["cycles"] == 5
    assert card._stdout[0] == b""


@pytest.mark.parametrize("failure_at", ["stage", "publish"])
def test_split_failure_invalidates_residency_and_closes_staging(
    failure_at, monkeypatch
):
    card = object.__new__(NodeCard)
    card.nodes = (0, 1)
    card.machine_json = "{}"
    card._eager_batch = None
    card.admission_cached = False
    card.log = None
    card._order, card._stdout = {}, {}
    card._publication_epoch = 0
    forgotten, closed = set(), set()
    card.residents = {
        node: SimpleNamespace(forget=lambda node=node: forgotten.add(node))
        for node in card.nodes
    }

    def prepare(node):
        if failure_at == "stage" and node == 1:
            raise OSError("upload failed")
        return SimpleNamespace(close=lambda: closed.add(node))

    def flush(node):
        if failure_at == "publish" and node == 1:
            raise OSError("tail failed")

    queues = {
        node: SimpleNamespace(
            prepare=lambda data, node=node: prepare(node),
            flush=lambda node=node: flush(node),
            submit=lambda *args, **kwargs: 1,
            discard_stdout=lambda: None,
            record=None,
        )
        for node in card.nodes
    }
    monkeypatch.setattr(card, "queues", queues, raising=False)
    recipe = SimpleNamespace(plain=lambda *args: b"plain")
    with pytest.raises(OSError):
        card.submit_wire(
            {node: (b"package", ()) for node in card.nodes},
            {node: (recipe, (), ()) for node in card.nodes},
        )
    assert forgotten == set(card.nodes)
    assert closed == ({0} if failure_at == "stage" else {0, 1})
