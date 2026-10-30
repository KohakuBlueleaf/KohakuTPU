"""HOSTL ABI integration: terminal acknowledgment owns every retained item."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeError, NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.native_exchange import NativeExchange
from kohakutpu.application.runtime.offline import HostMemory

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)


class Memory(HostMemory):
    fail = None

    def write_block(self, at, data):
        if at == self.fail:
            raise OSError("failed CQ acknowledgment")
        return super().write_block(at, data)


def setup():
    mem = Memory()
    allocated, unspilled = [], []

    def spill(size):
        at = 0x800000 + sum(allocated)
        allocated.append(size)
        return at

    queue = NodeQueue(
        mem,
        0,
        sq_n=4,
        cq_n=4,
        spill=spill,
        unspill=unspilled.append,
        record=LAYOUT.record,
        layout=LAYOUT,
    )
    queue.unspilled = unspilled
    queue.init()
    firmware = SpecFirmware(mem, 0, record=LAYOUT.record, layout=LAYOUT)
    firmware.boot()
    queue.idle = firmware.step
    package = queue.prepare(b"double package".ljust(32, b"\0"))
    execution = queue.prepare_list(
        [(package, [4096], 0, 0, 7), (package, [8192], 0, 0, 9)]
    )
    package.close()
    return mem, queue, firmware, execution


def test_hostl_list_runs_all_items_and_retires_only_after_ack():
    _mem, queue, firmware, execution = setup()
    tag = queue.submit_list(execution)
    execution.close()
    assert execution.inflight == 1
    assert not execution.descriptor.closed
    completion = queue.wait(tag)
    assert completion.record["list_index"] == 2
    assert completion.record["item_tag"] == 9
    assert completion.record["epoch"] == 0
    assert [(e[1], e[2]) for e in firmware.events if e[0] == "list-item"] == [
        (0, 7),
        (1, 9),
    ]
    assert execution.inflight == 0 and execution.descriptor.closed
    assert queue.counters["submitted"] == 1


def test_hostl_list_rejects_non_run_item_and_prints_failure():
    mem, queue, _, execution = setup()
    # Overwrite item 1's opcode with op_stop; firmware/double must refuse with BAD_OP.
    mem.write64(execution.descriptor.address + LAYOUT.sq_bytes, LAYOUT.op_stop)
    tag = queue.submit_list(execution)
    with pytest.raises(NodeError, match="BAD_OP") as exc:
        queue.wait(tag)
    c = exc.value.completion
    assert c.status == LAYOUT.bad_op and c.detail == LAYOUT.op_stop
    assert (c.record["list_index"], c.record["item_tag"], c.record["epoch"]) == (
        1,
        9,
        0,
    )
    assert (
        "[ka] package 9 failed: status 40 detail 2 step 0 value 0" in exc.value.console
    )
    # Header validation: detail 1 on epoch mismatch, detail 2 on version/flags mismatch.
    bad_epoch = queue.submit(
        None,
        op=LAYOUT.op_list,
        words=LAYOUT.list_tail(execution.descriptor.address, 2, 99),
    )
    with pytest.raises(NodeError, match="BAD_ARG") as exc_epoch:
        queue.wait(bad_epoch)
    assert exc_epoch.value.completion.detail == 1
    assert exc_epoch.value.completion.value == 99
    bad_flags = queue.submit(
        None,
        op=LAYOUT.op_list,
        words=LAYOUT.list_tail(
            execution.descriptor.address, 2, queue._generation, flags=1
        ),
    )
    with pytest.raises(NodeError, match="BAD_ARG") as exc_flags:
        queue.wait(bad_flags)
    assert exc_flags.value.completion.detail == 2
    execution.close()


def test_hostl_failed_ack_keeps_list_pins_and_poisoned_queue():
    mem, queue, firmware, execution = setup()
    run_tag = queue.submit(b"spilled package".ljust(32, b"\0"))
    tag = queue.submit_list(execution)
    execution.close()
    firmware.step()
    firmware.step()
    mem.fail = LAYOUT.cq_head
    with pytest.raises(OSError, match="acknowledgment"):
        queue.poll()
    assert queue._broken and execution.inflight == 1
    assert not execution.descriptor.closed and queue.unspilled == []
    assert run_tag not in queue._done and tag not in queue._done and queue.cq_head == 0


def test_native_list_cancellation_before_publication_owns_nothing():
    _, queue, _, execution = setup()
    native = NativeExchange.__new__(NativeExchange)
    native.queue = queue
    native.transport = SimpleNamespace(
        _c2h=1, _h2c=2, fd_generation=1, _native_lock=threading.RLock()
    )
    native.generation = 1

    def cancel():
        raise RuntimeError("cancel publish")

    with pytest.raises(RuntimeError, match="cancel publish"):
        native.submit(execution, cancel)
    assert execution.inflight == 0 and not queue._list_tags
    execution.close()
    assert execution.descriptor.closed


def test_native_list_does_not_overtake_unread_nop():
    _, queue, _, execution = setup()

    def forbidden(*args):
        pytest.fail("single-tag native endpoint overtook an outstanding NOP")

    queue.native_exchange = SimpleNamespace(submit=forbidden)
    old = queue.submit(op=LAYOUT.op_nop)
    new = queue.submit_list(execution, native=True)
    assert queue.wait(old).ok and queue.wait(new).ok
    execution.close()


def test_native_validation_failure_does_not_leak_unpublished_ownership():
    _, queue, _, execution = setup()
    native = NativeExchange.__new__(NativeExchange)
    native.queue = queue
    native.transport = SimpleNamespace(
        _c2h=1, _h2c=2, fd_generation=1, _native_lock=threading.RLock()
    )
    native.generation = 1

    def invalid(*args):
        raise ValueError("positive timeout required")

    native.exchange = invalid
    with pytest.raises(ValueError, match="timeout"):
        native.submit(execution, None)
    assert not queue._list_tags and not queue._outstanding
    assert execution.inflight == 0 and not queue._broken
    execution.close()
    assert execution.descriptor.closed
