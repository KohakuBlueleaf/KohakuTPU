"""Native poll reads preserve fatal/stdout wakeups, not only CQ progress."""

import os
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakutpu.application.runtime.card import SiliconCard
from kohakutpu.application.runtime.native_poll import NativePoll
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
PY_LAYOUT = L.Layout.from_machine(MACHINE)
LAYOUT = _rs.QueueLayout(PY_LAYOUT)


def poll_witness(wait):
    with tempfile.TemporaryFile() as memory:
        raw = bytearray(384)
        struct.pack_into("<Q", raw, 0, LAYOUT.st_fatal)
        struct.pack_into("<Q", raw, 24, 0x103)
        memory.write(raw)
        memory.flush()
        result, reads = wait(memory.fileno(), 0, 0, 0, 0.002, 0.00001, 0, LAYOUT)
        assert result == bytes(raw)
        assert reads == 1, "fatal state must wake without waiting for a CQ tail"
        struct.pack_into("<Q", raw, 0, LAYOUT.st_ready)
        struct.pack_into("<Q", raw, 320, 64)
        os.pwrite(memory.fileno(), raw, 0)
        result, reads = wait(memory.fileno(), 0, 0, 0, 0.002, 0.00001, 0, LAYOUT)
        assert result == bytes(raw)
        assert reads == 1, "stdout progress must wake without a CQ tail"


def test_native_poll_refuses_non_xdma_before_boot(monkeypatch):
    class OtherTransport:
        closed = False

        def close(self):
            self.closed = True

    transport = OtherTransport()
    monkeypatch.setenv("KTPU_NATIVE_POLL", "1")
    with pytest.raises(ValueError, match="Linux XDMA"):
        SiliconCard("unused-before-boot", transport=transport)
    assert transport.closed


def test_native_poll_fatal_and_stdout_are_wake_conditions():
    poll_witness(_rs.wait_tail)
    with pytest.raises(ValueError, match="QueueLayout"):
        _rs.wait_tail(0, 0, 0, 0, 0.001, 0.0, 1, None)
    with pytest.raises(TypeError):
        _rs.wait_tail(0, 0, 0, 0, 0.001, 0.0, 1, PY_LAYOUT)
    # NativePoll builds and holds _rs.QueueLayout once at construction.
    with tempfile.TemporaryFile() as memory:
        raw = bytearray(LAYOUT.fw + 384)
        struct.pack_into("<Q", raw, LAYOUT.fw, LAYOUT.st_fatal)
        memory.write(raw)
        memory.flush()
        queue = SimpleNamespace(
            layout=PY_LAYOUT,
            base=0,
            cq_head=0,
            so_rd=0,
            mem=SimpleNamespace(_map=lambda addr, span: addr),
        )
        transport = SimpleNamespace(
            _c2h=memory.fileno(),
            counts={"c2h": 0, "c2h_bytes": 0},
            _retry=lambda kind, addr, span, fn: fn(addr, span),
        )
        poller = NativePoll(queue, transport)
        assert isinstance(poller.layout, _rs.QueueLayout)
        queue.layout = None
        got, reads = poller(0.001)
        assert reads == 1 and got == bytes(raw[LAYOUT.fw :])


def test_mutant_poll_only_wakes_for_completion_is_caught():
    assert "bindings.poll/cq_only" in rs.mutants()
    with (
        rs.mutant("bindings.poll/cq_only"),
        pytest.raises(AssertionError, match="fatal state"),
    ):
        poll_witness(_rs.wait_tail)
