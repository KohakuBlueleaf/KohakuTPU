"""Immutable host-input identity guards deterministic host computation recipes."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakutpu.application.ktpugrad import recipe as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.storage import Storage
from kohakutpu.stack import rs


def witness(monkeypatch, tmp_path):
    monkeypatch.setenv("KTPU_HOST_RECIPE", "1")
    monkeypatch.setenv("KTPU_NATIVE_PROGRAM", "1")
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    source, dest = Storage(dev, 256), Storage(dev, 256)
    source.host = bytes([3]) * 256
    plan = SimpleNamespace(ast=object(), out_slot=1)
    calls = []

    def execute(dev, plan, buffers):
        calls.append(1)
        raw = bytes((x + 1) % 256 for x in buffers[0].host)
        dev.write(buffers[1].addr, raw)
        buffers[1].host = raw
        return []

    table = R.Recipes()
    try:
        with patch.object(R.run, "execute", execute):
            table.execute(dev, plan, {0: source, 1: dest})
            dest.host = None
            table.execute(dev, plan, {0: source, 1: dest})
            assert len(calls) == 1
            assert dev.read(dest.addr, 256) == bytes([4]) * 256
            source.host = bytes([7]) * 256
            dest.host = None
            table.execute(dev, plan, {0: source, 1: dest})
            assert (
                dev.read(dest.addr, 256) == bytes([8]) * 256
            ), "stale host recipe after input replacement"
            assert len(calls) == 2
        rejected = R.Recording(
            dev,
            {0: source, 1: dest},
            native=table.native_program,
            host_recipe=table.host_recipe,
        )
        monkeypatch.setattr(R, "HOST_BYTES", 1)
        with patch.object(R.run, "execute", execute):
            rejected.capture(plan, {0: source, 1: dest})
        assert rejected.unsupported == "host byte budget"
        rejected.close()
    finally:
        table.clear()
        source.free()
        dest.free()
        dev.close()


def test_host_recipe_changed_input_and_byte_budget(monkeypatch, tmp_path):
    witness(monkeypatch, tmp_path)


def test_oversized_host_inputs_keep_ordinary_recipe_path(monkeypatch):
    monkeypatch.setenv("KTPU_HOST_RECIPE", "1")
    monkeypatch.setenv("KTPU_NATIVE_PROGRAM", "1")
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    source = Storage(dev, 256)
    source.host = bytes(R.HOST_BYTES + 1)
    plan = SimpleNamespace(ast=object(), out_slot=0)
    table = R.Recipes()
    calls = []

    def execute(*args):
        calls.append(1)
        return []

    try:
        with patch.object(R.run, "execute", execute):
            table.execute(dev, plan, {0: source})
            table.execute(dev, plan, {0: source})
        assert len(calls) == 1, "host budget rejected an ordinary address-only recipe"
    finally:
        table.clear()
        source.free()
        dev.close()


def test_mutant_missing_host_identity_is_caught(monkeypatch, tmp_path):
    assert "app.recipe/no_host_identity" in rs.mutants()
    with (
        rs.mutant("app.recipe/no_host_identity"),
        pytest.raises(AssertionError, match="stale host"),
    ):
        witness(monkeypatch, tmp_path)
