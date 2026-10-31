"""What a card decides by itself: the resident knob and one-node submission."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.card.common import ResidentMode, parse_resident_mode


@pytest.mark.parametrize(
    "value, mode",
    [
        (None, ResidentMode.UNSET),
        ("0", ResidentMode.OFF),
        ("1", ResidentMode.ON),
        ("yes", ResidentMode.ON),
    ],
)
def test_the_resident_knob(value, mode):
    assert parse_resident_mode(value) is mode


def test_a_device_latches_the_resident_knob(monkeypatch):
    monkeypatch.setenv(D.RESIDENT_ENV, "1")
    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    try:
        monkeypatch.setenv(D.RESIDENT_ENV, "0")
        assert dev.resident_mode is ResidentMode.ON
    finally:
        dev.close()


def test_a_wire_on_the_second_node_waits_on_that_node():
    card = object.__new__(NodeCard)
    card.nodes = (0, 1)
    card.debug = False
    card.log = None
    card.timeout = 1.0
    card.runs = 0
    card.machine_json = "{}"
    card._order, card._stdout, card._segs = {}, {}, {}
    card.residents = {}

    def queue(cycles):
        return SimpleNamespace(
            flush=lambda: None,
            wait=lambda tag, timeout=1.0: SimpleNamespace(cycles=cycles, record=None),
            read_stdout=lambda: "",
            record=None,
        )

    card.queues = {0: queue(10), 1: queue(42)}
    card.q = card.queues[0]
    card._submit = lambda n, data, bindings, admitted=False: 7
    recipe = SimpleNamespace(plain=lambda h, t: b"plain")
    pending = card.submit_wire({1: (b"wire", ())}, {1: (recipe, [0], [])})
    got = card.collect(pending)
    assert got["cycles"] == 42 and got["nodes"][1]["cycles"] == 42
