"""Offline witnesses for target selection and serial simulator ownership."""

from types import SimpleNamespace

import numpy as np
import pytest
import test_spec as suite
from harness import simulators, vectors
from harness.fresh import CARDS
from runners.target import isa, l0, l2


def test_release_closes_and_untracks_all_cached_models(monkeypatch):
    closed = []
    card = SimpleNamespace(t=SimpleNamespace(close=lambda: closed.append("card")))
    dev = SimpleNamespace(
        card=SimpleNamespace(t=SimpleNamespace(close=lambda: closed.append("device")))
    )
    monkeypatch.setattr(l0, "_CARDS", {1: card})
    monkeypatch.setattr(l2, "_VERILATED", {("board", 1, "build"): dev})
    CARDS.append(card)
    try:
        simulators.release()
        assert sorted(closed) == ["card", "device"]
        assert not l0._CARDS and not l2._VERILATED
        assert all(held is not card for held in CARDS)
    finally:
        CARDS[:] = [held for held in CARDS if held is not card]
        l0._CARDS.clear()
        l2._VERILATED.clear()


def test_retaining_models_fails_the_release_witness(monkeypatch):
    monkeypatch.setattr(simulators, "release", lambda: None)
    with pytest.raises(AssertionError):
        test_release_closes_and_untracks_all_cached_models(monkeypatch)


def test_l2_opens_the_board_of_the_selected_simulator(monkeypatch):
    opened = []
    monkeypatch.setenv("KTPU_BOARD", "simulated-board")
    monkeypatch.setenv("KTPU_SPEC_VERILATOR", "selected-build")
    monkeypatch.setattr(l2, "_VERILATED", {})

    def device(**kw):
        opened.append(kw)
        return SimpleNamespace(card=None, close=lambda: None)

    monkeypatch.setattr(l2, "Device", device)
    monkeypatch.setattr(l2, "track", lambda card: card)
    monkeypatch.setattr(l2, "_on_device", lambda dev, inp: {"match": True})
    assert l2.run_verilated({}, {"vector": {"machine": "silicon-board"}}) == {
        "match": True
    }
    assert opened == [
        {
            "kind": "sim",
            "build": "selected-build",
            "nodes": 1,
            "board": "simulated-board",
        }
    ]


def test_ignoring_the_model_board_fails_the_selection_witness(monkeypatch):
    original = l2.verilated

    def silicon_board(inp, ctx):
        monkeypatch.delenv("KTPU_BOARD", raising=False)
        return original(inp, ctx)

    monkeypatch.setattr(l2, "verilated", silicon_board)
    with pytest.raises(AssertionError):
        test_l2_opens_the_board_of_the_selected_simulator(monkeypatch)


def test_l2_executes_virtual_bindings_through_device_run(monkeypatch):
    calls, freed = [], []
    shape = SimpleNamespace(
        nbytes=32,
        space="dram",
        layout={"node": None},
        host_shape=(16,),
        host_dtype=np.float16,
        pack=lambda array: array.tobytes(),
        unpack=lambda raw: raw,
    )
    monkeypatch.setattr(l2.Buffer, "of", lambda *args: shape)
    addresses = iter((0x1000000000000000, 0x1000000000000020))
    dev = SimpleNamespace(
        card=SimpleNamespace(machine_json="unused"),
        alloc=lambda *args: next(addresses),
        write=lambda *args: None,
        run=lambda compiled, binds: calls.append(dict(binds)),
        read=lambda addr, size: bytes(size),
        free=freed.append,
    )
    compiled = SimpleNamespace(
        l1=SimpleNamespace(
            body=lambda *args: SimpleNamespace(params=[("x", "f16"), ("y", "f16")])
        )
    )
    _, out = l2._run_l1(dev, compiled, "kernel", {"inputs": ["x"], "output": "y"})
    assert calls == [{"x": 0x1000000000000000, "y": 0x1000000000000020}]
    assert freed == list(calls[0].values()) and out == bytes(32)


def test_standalone_mutant_checks_its_baseline(monkeypatch):
    v = vectors.Vector(
        vectors.ROOT / "broken.json",
        {"tier": 2, "status": "frozen", "runner": "unused", "expect": {"pass": True}},
    )
    monkeypatch.setattr(suite, "BASELINE", {})
    monkeypatch.setattr(suite, "context", lambda *args: {})
    monkeypatch.setattr(suite, "run", lambda *args: {"pass": False})
    with pytest.raises(AssertionError):
        suite.test_mutant_fails(v, "unused", None, lambda *args: None)


def test_failed_isa_bench_keeps_its_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(
        isa.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="FAIL missing fixtures", stderr=""
        ),
    )
    assert isa.bench({"bench": "sru_core_mid"}, {}) == {"pass": False}
    assert "FAIL missing fixtures" in capsys.readouterr().out
