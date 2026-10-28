"""Offline and attached arena sizing uses the named machine's DRAM capacity."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import offline as O
from kohakutpu.application.tools import bringup
from kohakutpu.driver import host
from kohakutpu.language import machine


@pytest.mark.parametrize("name", ["multimesh_v9t6", "vlt_v9t6s_1n"])
def test_offline_device_reports_the_machine_capacity(name):
    dev = D.Device(kind="offline", board=name)
    size = machine.load(name).get("card.dram.bytes")
    assert dev.dram_bytes == dev.card.dram_bytes == size
    assert dev.memory.dram.hi == size - host.SCRATCH_RESERVE


@pytest.mark.parametrize("size", [64 << 20, 512 << 20])
def test_offline_arena_tracks_a_changed_schema(monkeypatch, size):
    facts = machine.load("multimesh_v9t6").dict()
    facts["card"]["dram"]["bytes"] = size
    monkeypatch.setattr(host, "load_machine", lambda _: machine.Machine(facts))
    card = O.OfflineCard("multimesh_v9t6")
    assert card.dram_bytes == size
    assert card.memory.dram.hi == size - host.SCRATCH_RESERVE


@pytest.mark.parametrize("stage", [False, True])
def test_bringup_uses_the_machine_limit_without_a_model_wrap(monkeypatch, stage):
    monkeypatch.setattr(bringup, "SiliconCard", lambda *args, **kw: kw)
    a = SimpleNamespace(
        board="vlt_v9t6s_1n", build="unused", space="stage" if stage else "dram"
    )
    monkeypatch.setattr(bringup, "idle_of", lambda *_: None)
    got = bringup.open_card(a, object(), [0])
    size = machine.load(a.board).get("card.dram.bytes")
    assert got["arena_end"] == (C.ARENA if stage else size - host.SCRATCH_RESERVE)


def test_wrong_model_capacity_is_refused_before_boot(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(
        C,
        "VerilatorTransport",
        lambda *a, **kw: SimpleNamespace(close=lambda: closed.append(1)),
    )
    monkeypatch.setattr(
        C, "SimDram", lambda *a: SimpleNamespace(dram=[("dram", "mem", 1, 64)])
    )
    with pytest.raises(ValueError, match="model DRAM .* != machine DRAM"):
        C.VerilatorCard(tmp_path, board="vlt_v9t6s_1n", log=tmp_path / "model.log")
    assert closed == [1]
