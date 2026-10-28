"""Failure records retain original, not rerun, engine/mover observations."""

import json
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import Completion, NodeError
from kohakutpu.application.runtime.card import NodeCard, Pending

CONSOLE = (
    "PKGF de stat de01064301140040 moves 1800000014 mvstat 100000014 xfdata 0\n"
    "PKGF de ctr 0 1 2 3 4 5 6 7 8 9 a b c d e f\n"
)


def failed_card(nodes):
    card = object.__new__(NodeCard)
    card.nodes, card.debug, card.runs, card._timeout = nodes, False, 3, 1
    card._segment = lambda *args: ""
    errors = {}
    card.queues = {}
    for n in nodes:
        error = NodeError(Completion(7 + n, 0x30, 11, 2, 100, 0), CONSOLE)
        errors[n] = error

        def wait(*args, error=error, **kwargs):
            raise error

        card.queues[n] = SimpleNamespace(wait=wait)
    card.q = card.queues[nodes[0]]
    raw = b"bound package bytes"
    pkg = SimpleNamespace(to_bytes=lambda: raw)
    pending = Pending(
        len(nodes) == 1,
        {n: (7 + n, pkg) for n in nodes},
        {n: (pkg, [0x100000 + n]) for n in nodes},
    )
    return card, pending, errors, raw


@pytest.mark.parametrize("nodes", [(0,), (0, 1)])
def test_failure_json_from_collect(tmp_path, monkeypatch, nodes):
    monkeypatch.setenv("KOHAKUTPU_KEEP_FAILED", str(tmp_path))
    card, pending, errors, raw = failed_card(nodes)
    if len(nodes) == 1:
        with pytest.raises(NodeError) as caught:
            card.collect(pending)
        assert caught.value is errors[0]
    else:
        got = card.collect(pending, faults=True)
        for n in nodes:
            assert got["nodes"][n]["snapshot"] == errors[n].snapshot
    records = sorted(tmp_path.glob("*.json"))
    assert len(records) == len(nodes)
    for path in records:
        saved = json.loads(path.read_text(encoding="utf-8"))
        n = saved["node"]
        assert saved["bindings"] == [0x100000 + n]
        assert saved["snapshot"] == errors[n].snapshot
        assert saved["snapshot"]["moves"] == {"issued": 24, "done": 20}
        assert saved["completion"]["tag"] == 7 + n
        assert saved["console"] == CONSOLE
        assert path.with_suffix(".pkg").read_bytes() == raw


def test_bindings_only_mutant_is_caught(tmp_path, monkeypatch):
    original = NodeCard._keep_failed

    def bindings_only(self, pkg, bindings, error, node):
        original(self, pkg, bindings, error, node)
        for path in tmp_path.glob("*.json"):
            path.write_text(json.dumps(bindings), encoding="utf-8")

    monkeypatch.setattr(NodeCard, "_keep_failed", bindings_only)
    with pytest.raises((AssertionError, TypeError)):
        test_failure_json_from_collect(tmp_path, monkeypatch, (0,))
