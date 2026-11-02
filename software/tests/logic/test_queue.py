"""The native queue: its RUN bytes, its failed publications, its poll's wake conditions."""

import importlib.util
import os
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import client as CLIENT
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NAP_SHARE, FirmwareFatal, NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.stack.rs import _rs

MACHINE = Path(__file__).resolve().parents[3] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)
NATIVE = _rs.QueueLayout(LAYOUT)


class Memory(HostMemory):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.fail_at = None

    def write_block(self, addr, data):
        if addr == self.fail_at:
            raise OSError("publication transport failure")
        self.writes.append((addr, bytes(data)))
        return super().write_block(addr, data)


def transcript(native):
    mem = Memory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, layout=LAYOUT)
    q.native_run = _rs.QueueRun(LAYOUT) if native else None
    q.init()
    fw = SpecFirmware(mem, 0, layout=LAYOUT)
    fw.boot()
    q.idle = fw.step
    mem.writes.clear()
    for i in range(5):
        tag = q.submit(b"protocol double".ljust(32, b"\0"), [0x1000 + i * 64, 0])
        assert q.wait(tag).ok
    return mem.writes, q.counters


def test_native_run_bytes_and_ring_wrap_are_python_s():
    assert transcript(True) == transcript(False)


def test_a_failed_publication_keeps_its_pins():
    mem = Memory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, layout=LAYOUT)
    q.native_run = _rs.QueueRun(LAYOUT)
    q.init()
    mem.fail_at = q.geo["sq"]
    with pytest.raises(OSError, match="publication"):
        q.submit(b"package".ljust(32, b"\0"))
        q.flush()
    assert q._broken and q._outstanding == {1}
    assert 1 in q._package_tags


class Timeline:
    """A node that finishes each entry at a set time on a fake clock, and a
    host that sleeps on that clock."""

    def __init__(self, hz, finish, poll=0.02, spin=0.05):
        self.now = 100.0
        self.finish: float | None = finish
        self.seen = 0.0
        self.sleeps = []
        self.spilled = 1 << 21
        mem = Memory()

        def spill(size):
            self.spilled += 4096
            return self.spilled

        self.q = q = NodeQueue(
            mem,
            0,
            sq_n=4,
            cq_n=4,
            spill=spill,
            record=LAYOUT.record,
            layout=LAYOUT,
            clock=lambda: self.now,
            spin=spin,
            nap=poll,
            hz=hz,
        )
        q.init()
        self.fw = SpecFirmware(mem, 0, record=LAYOUT.record, layout=LAYOUT)
        self.fw.boot()
        poll_once = q.poll

        def timed_poll(wait_seconds: float = 0):
            if self.finish is not None and self.now >= self.finish:
                self.finish = None
                self.fw.step()
                self.seen = self.now
            return poll_once(wait_seconds)

        q.poll = timed_poll

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_a_long_wait_nothing_predicted_sees_its_completion_soon():
    line = Timeline(200e6, 100.0632)
    with patch.object(CLIENT.time, "sleep", line.sleep):
        line.q.wait(line.q.submit(b"p".ljust(32, b"\0")))
    assert line.seen - 100.0 < 0.0632 * (1 + NAP_SHARE) + 0.0001


def run_list(line, seconds):
    package = line.q.prepare(b"p".ljust(32, b"\0"))
    execution = line.q.prepare_list([(package, (), 0, 0, 1)])
    line.fw.cycles = int(seconds * 200e6)
    line.finish = line.now + seconds
    with patch.object(CLIENT.time, "sleep", line.sleep):
        line.q.wait(line.q.submit_list(execution))
    return execution


def test_a_repeated_list_sleeps_to_its_due_time_and_polls_finely():
    line = Timeline(200e6, None)
    seconds = 0.0761
    execution = run_list(line, seconds)
    assert execution.seconds == pytest.approx(seconds)
    line.fw.cycles = int(seconds * 200e6)
    start = line.now
    line.finish = start + seconds
    polls = line.q.counters["polls"]
    with patch.object(CLIENT.time, "sleep", line.sleep):
        line.q.wait(line.q.submit_list(execution))
    assert line.seen - start < seconds + 1e-3
    # Naps to the due time, then a poll every 50 us at most.
    assert line.q.counters["polls"] - polls < 300
    assert max(line.sleeps[-300:]) <= 0.02


def test_a_list_that_overruns_its_due_time_is_still_polled_finely():
    line = Timeline(200e6, None)
    execution = run_list(line, 0.08)
    line.finish = line.now + 0.09
    start = line.now
    with patch.object(CLIENT.time, "sleep", line.sleep):
        line.q.wait(line.q.submit_list(execution))
    assert line.seen - start < 0.09 + 1e-3


def test_a_long_list_is_polled_finely_well_past_its_due_time():
    # Due at 0.95 of 1.2 s less 0.5 ms: completion comes past due + `spin`.
    line = Timeline(200e6, None)
    execution = run_list(line, 1.2)
    line.fw.cycles = int(1.2 * 200e6)
    start = line.now
    line.finish = start + 1.2
    with patch.object(CLIENT.time, "sleep", line.sleep):
        line.q.wait(line.q.submit_list(execution))
    assert line.seen - start < 1.2 + 1e-3


def test_a_fault_ends_a_predicted_sleep_at_the_next_nap():
    line = Timeline(200e6, None)
    execution = run_list(line, 1.0)
    fatal = 0x103
    tag = line.q.submit_list(execution)
    line.fw.wr(LAYOUT.fw + 8 * LAYOUT.fw_fatal, fatal)
    line.fw.wr(LAYOUT.fw + 8 * LAYOUT.fw_state, LAYOUT.st_fatal)
    start = line.now
    with patch.object(CLIENT.time, "sleep", line.sleep), pytest.raises(FirmwareFatal):
        line.q.wait(tag)
    assert line.now == start


@pytest.mark.parametrize("chain", ["0", "1"])
def test_the_chain_knob_reaches_the_list_entry_and_is_served(chain, monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_LIST_CHAIN", chain)
    line = Timeline(200e6, None)
    package = line.q.prepare(b"p".ljust(32, b"\0"))
    execution = line.q.prepare_list([(package, (), 0, 0, 1)])
    assert execution.flags == int(chain) * L.LIST_F_CHAIN
    tag = line.q.submit_list(execution)
    line.q.flush()
    word = line.fw.rd(line.q.geo["sq"] + 8 * LAYOUT.list_flags)
    assert word == execution.flags
    line.finish = line.now
    with patch.object(CLIENT.time, "sleep", line.sleep):
        assert line.q.wait(tag).ok


def test_a_list_flag_past_the_chain_bit_is_refused():
    line = Timeline(200e6, None)
    package = line.q.prepare(b"p".ljust(32, b"\0"))
    execution = line.q.prepare_list([(package, (), 0, 0, 1)])
    execution.flags = L.LIST_F_CHAIN << 1
    tag = line.q.submit_list(execution)
    line.q.flush()
    line.finish = line.now
    with (
        patch.object(CLIENT.time, "sleep", line.sleep),
        pytest.raises(CLIENT.NodeError) as refused,
    ):
        line.q.wait(tag)
    assert refused.value.completion.status == LAYOUT.bad_arg


WAIT_MUTANTS = {
    "flat_nap": (
        "time.sleep(min(self.nap, rest))",
        "time.sleep(self.nap)",
        test_a_long_wait_nothing_predicted_sees_its_completion_soon,
    ),
    "no_prediction": (
        "left = due - start[0] - waited",
        "left = -waited",
        test_a_repeated_list_sleeps_to_its_due_time_and_polls_finely,
    ),
    "no_due_end": (
        "rest = left if left > 0 else waited * NAP_SHARE",
        "rest = self.nap",
        test_a_repeated_list_sleeps_to_its_due_time_and_polls_finely,
    ),
}


def test_the_client_source_holds_every_wait_mutant():
    source = Path(CLIENT.__file__).read_text(encoding="utf-8")
    assert all(old in source for old, _, _ in WAIT_MUTANTS.values())


@pytest.mark.parametrize("name", WAIT_MUTANTS)
def test_each_wait_mutant_is_told(name, tmp_path):
    old, new, check = WAIT_MUTANTS[name]
    source = Path(CLIENT.__file__).read_text(encoding="utf-8").replace(old, new)
    changed = tmp_path / "client.py"
    changed.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(CLIENT.__name__, changed)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with (
        patch.object(sys.modules[__name__], "NodeQueue", module.NodeQueue),
        pytest.raises(AssertionError),
    ):
        check()


def test_fatal_and_stdout_wake_the_poll():
    with tempfile.TemporaryFile() as memory:
        raw = bytearray(384)
        struct.pack_into("<Q", raw, 0, NATIVE.st_fatal)
        struct.pack_into("<Q", raw, 24, 0x103)
        memory.write(raw)
        memory.flush()
        args = (memory.fileno(), 0, 0, 0, 0.002, 0.00001, 0, NATIVE)
        assert _rs.wait_tail(*args) == (bytes(raw), 1)
        struct.pack_into("<Q", raw, 0, NATIVE.st_ready)
        struct.pack_into("<Q", raw, 320, 64)
        os.pwrite(memory.fileno(), raw, 0)
        assert _rs.wait_tail(*args) == (bytes(raw), 1)
