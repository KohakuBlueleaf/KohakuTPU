"""No image or shared-memory write races a previous CPU owner."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    software = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(software / "kohakuaccel/os/tools"))
    monkeypatch.syspath_prepend(str(software / "kohakutpu/os/tools"))
    return importlib.import_module("run_card")


class Window:
    def __init__(self, events, node):
        self.events, self.node = events, node

    def stop(self):
        self.events.append(("stop", self.node))


def test_runner_stops_every_node_before_loading_and_after_a_failure(
    runner, monkeypatch, tmp_path
):
    events = []
    windows = [Window(events, 0), Window(events, 1)]
    card = SimpleNamespace(
        m={"ctrl": [0, 1]},
        t=SimpleNamespace(close=lambda: events.append(("close",))),
    )
    monkeypatch.setattr(runner, "Card", lambda *_: card)
    monkeypatch.setattr(
        runner.rv64load,
        "LoadWindow",
        lambda t, addr: windows[addr - runner.rv64load.WINDOW_OFFSET],
    )

    def load(*_):
        assert events == [("stop", 0), ("stop", 1)]
        events.append(("load",))
        raise ValueError("image failure")

    monkeypatch.setattr(runner, "_run_loaded", load)
    args = SimpleNamespace(out=tmp_path, model=None, xdma=True)
    with pytest.raises(ValueError, match="image failure"):
        runner.run(args, {"nodes": 2, "mesh_of": [0, 1]})
    assert events == [("stop", 0), ("stop", 1), ("load",)] + [
        ("stop", 0),
        ("stop", 1),
        ("close",),
    ]


@pytest.mark.parametrize("failed_node", [0, 1])
def test_cleanup_attempts_other_nodes_after_a_stop_failure(runner, failed_node):
    events = []
    failure = OSError("stop transport failure")

    class FailingWindow(Window):
        def stop(self):
            super().stop()
            if self.node == failed_node:
                raise failure

    windows = [FailingWindow(events, 0), FailingWindow(events, 1)]
    card = SimpleNamespace(t=SimpleNamespace(close=lambda: events.append(("close",))))
    with pytest.raises(OSError) as caught:
        runner._close_card(card, windows)
    assert caught.value is failure
    assert events == [("stop", 0), ("stop", 1), ("close",)]


def test_model_log_failure_still_attempts_every_stop(runner, monkeypatch, tmp_path):
    model = importlib.import_module("ostpu.model")
    events = []

    class FailingWindow(Window):
        def stop(self):
            super().stop()
            raise OSError("stop transport failure")

    def read(*_):
        raise ValueError("log read failure")

    owner = model.ModelResidents.__new__(model.ModelResidents)
    owner.closed = False
    owner.windows = [FailingWindow(events, 0), Window(events, 1)]
    owner.card = SimpleNamespace(
        read=read, t=SimpleNamespace(close=lambda: events.append(("close",)))
    )
    owner.spec, owner.out = {}, tmp_path
    monkeypatch.setattr(model, "regions", lambda *_: [(0, 4096)])
    with pytest.raises(OSError, match="stop transport failure"):
        owner.close()
    assert events == [("stop", 0), ("stop", 1), ("close",)]
