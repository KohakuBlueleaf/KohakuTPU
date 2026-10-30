"""Warm native submission must not call back into Python, including retirement."""

import os
import struct
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.eager_batch import batching
from kohakutpu.application.runtime.native_launch import descriptor
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX native upload")


def setup(tmp_path, offset=0):
    with patch.dict(os.environ, {"KTPU_NATIVE_MEMORY": "1", "KTPU_WIRE_RECIPE": "1"}):
        dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
        kernel, given = V.flat_kernel(
            X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2
        )
        compiled = dev.specialise(kernel, given)
        names = compiled.l1.params(compiled.name, "l1")
        handles = [dev.alloc(4096) for _ in names]
        binds = dict(zip((p for p, _ in names), handles, strict=True))
        captured = []

        def capture(compiled, full, progs, submitted):
            captured.append(descriptor(dev, compiled, full, progs, submitted, binds))

        with patch.object(dev, "_recipe_capture", capture):
            dev.run(compiled, binds)
    path = tmp_path / "queue"
    path.write_bytes(bytes(4 << 20))
    fd = os.open(path, os.O_RDWR)
    layout = dev.card.queue_layout
    geo = layout.geometry(4, 4, 4096, 4096)
    os.pwrite(fd, layout.info_line(layout.version, 4, 4, 4096, 4096, 1), layout.info)
    os.pwrite(fd, layout.line_bytes(layout.st_ready), layout.fw)
    queue = SimpleNamespace(
        layout=layout,
        _native_lock=threading.RLock(),
        timeout=1.0,
        version=layout.version,
        _generation=1,
        _broken=False,
        _outstanding=set(),
        _staged=[],
        sq_tail=0,
        cq_head=0,
        so_rd=0,
        _tag=0,
        console="",
        console_at=0,
        counters=dict(uploads=0, upload_bytes=0, submitted=0, polls=0),
    )
    transport = SimpleNamespace(
        _c2h=fd, _h2c=fd, fd_generation=1, _native_lock=threading.RLock()
    )
    dev.card._publication_epoch = 0
    direct = _rs.DirectWire(
        queue,
        transport,
        dev.memory,
        0,
        offset,
        (4, 4, geo["cq"], geo["rec"], geo["so"]),
        {},
    )
    dev.card.native_direct = direct
    return dev, captured[0], handles, fd, geo, queue


def completion(fd, geo, tag, head, status=0, layout=None):
    os.pwrite(fd, struct.pack("<4Q", tag, status, 123, 0), geo["cq"] + head % 4 * 32)
    os.pwrite(
        fd, struct.pack("<8Q", tag, *([0] * 7)) + bytes(64), geo["rec"] + head % 4 * 128
    )
    os.pwrite(
        fd,
        struct.pack("<8Q", head + 1, *([0] * 7)),
        0x100 if layout is None else layout.cq_tail,
    )
    os.pwrite(
        fd,
        struct.pack("<8Q", head, *([0] * 7)),
        0x0C0 if layout is None else layout.sq_head,
    )


def test_direct_native_launch_has_no_python_callbacks_and_retires(tmp_path):
    dev, native, handles, fd, geo, queue = setup(tmp_path)
    try:
        completion(fd, geo, 1, 0)
        first = native.launch(dev, handles)
        assert first.run.cycles == 123
        slot = dev.card.native_direct.slot
        calls = []

        def profile(frame, event, arg):
            if event == "call":
                calls.append(frame.f_code.co_name)

        completion(fd, geo, 2, 1)
        before = dev.counters["runs"]
        sys.setprofile(profile)
        try:
            result = native.launch(dev, handles)
        finally:
            sys.setprofile(None)
        assert calls == [], f"Python callbacks inside native launch: {calls}"
        assert result.run.cycles == 123 and result.references == []
        assert not dev.inflight and dev.counters["runs"] == before + 1
        assert dev.card.native_direct.slot == slot and queue.sq_tail == 2
        assert os.pread(fd, 64, slot) != bytes(64)
        assert all(count == 0 for count in dev.memory.dram.references.values())
    finally:
        os.close(fd)
        dev.close()


def test_native_host_write_program_has_no_python_callbacks(tmp_path):
    dev, _, handles, fd, _, _ = setup(tmp_path)
    raw = bytes(range(64)) * 4
    program = _rs.LaunchProgram(
        [("write", ("literal", handles[0], None, 0), raw)],
        [],
        0,
        (dev.recipe_epoch, dev.recipe_epoch.generation),
    )
    calls = []

    def profile(frame, event, arg):
        if event == "call":
            calls.append(frame.f_code.co_name)

    try:
        _, at = dev.mmu.resolve(handles[0])
        sys.setprofile(profile)
        try:
            program.launch(dev, [])
        finally:
            sys.setprofile(None)
        assert not calls, calls
        assert os.pread(fd, len(raw), at) == raw
        # Short writes must keep the existing padding/merge behavior, not overwrite64bytes.
        short = _rs.LaunchProgram(
            [("write", ("literal", handles[0], None, 0), b"hi")],
            [],
            0,
            (dev.recipe_epoch, dev.recipe_epoch.generation),
        )
        observed = []
        with patch.object(
            dev, "write", lambda address, data: observed.append((address, data))
        ):
            short.launch(dev, [])
        assert observed == [(handles[0], b"hi")]
    finally:
        os.close(fd)
        dev.close()


def test_native_program_retirement_is_callback_free(tmp_path):
    dev, native, handles, fd, geo, _ = setup(tmp_path)
    program = _rs.LaunchProgram(
        [("native", native, [("literal", h, None, 0) for h in handles])],
        [],
        0,
        (dev.recipe_epoch, dev.recipe_epoch.generation),
    )
    try:
        completion(fd, geo, 1, 0)
        program.launch(dev, [])
        completion(fd, geo, 2, 1)
        calls = []

        def profile(frame, event, arg):
            if event == "call":
                calls.append(frame.f_code.co_name)

        sys.setprofile(profile)
        try:
            runs = program.launch(dev, [])
        finally:
            sys.setprofile(None)
        assert calls == [] and runs[0].cycles == 123 and not dev.inflight
        assert "bindings.submit/python_publication" in rs.mutants()
        completion(fd, geo, 3, 2)
        with rs.mutant("bindings.submit/python_publication"):
            calls.clear()
            sys.setprofile(profile)
            try:
                program.launch(dev, [])
            finally:
                sys.setprofile(None)
        assert "submit_wire" in calls, "Python publication escaped the observation"
    finally:
        os.close(fd)
        dev.close()


def test_direct_respects_deferred_batch_and_owner_invalidation(tmp_path):
    dev, native, handles, fd, _, queue = setup(tmp_path)
    try:

        def collect(pending):
            return (
                pending.owner.result(pending) if hasattr(pending, "owner") else pending
            )

        with (
            patch.object(dev.card, "collect", collect, create=True),
            patch.object(dev.card, "submit", lambda *args: None, create=True),
        ):
            with batching(dev.card):
                pending = native.launch(dev, handles)
                assert queue.sq_tail == 0 and pending.run is None
            dev.collect_through(pending)
        dev.invalidate_library()
        with pytest.raises(ValueError, match="invalidated"):
            native.launch(dev, handles)
        assert queue.sq_tail == 0
    finally:
        os.close(fd)
        dev.close()


def test_direct_closed_owner_cannot_republish_after_arena_shutdown(tmp_path):
    dev, native, handles, fd, geo, _ = setup(tmp_path)
    try:
        completion(fd, geo, 1, 0)
        native.launch(dev, handles)
        endpoint = dev.card.native_direct
        dev.close()
        with pytest.raises(RuntimeError, match="closed"):
            endpoint.submit(dev.card, bytes(64))
    finally:
        os.close(fd)


def test_direct_maps_host_upload_but_keeps_device_address(tmp_path):
    offset = 8 << 20
    dev, native, handles, fd, geo, queue = setup(tmp_path, offset)
    try:
        completion(fd, geo, 1, 0)
        native.launch(dev, handles)
        at = dev.card.native_direct.slot
        entry = struct.unpack("<8Q", os.pread(fd, 64, queue.layout.sq))
        assert entry[queue.layout.run_pkg] == at
        assert os.pread(fd, 64, offset + at) != bytes(64)
        assert os.pread(fd, 64, at) == bytes(64)
    finally:
        os.close(fd)
        dev.close()


def test_direct_reset_refuses_before_publication(tmp_path):
    dev, native, handles, fd, _, queue = setup(tmp_path)
    try:
        queue._generation += 1
        with pytest.raises(RuntimeError, match="queue generation"):
            native.launch(dev, handles)
        assert queue.sq_tail == 0 and not dev._uncertain
        assert not any(dev.memory.dram.references.values())
    finally:
        os.close(fd)
        dev.close()


def queue_ownership_witness(tmp_path, synchronize=True):
    dev, native, handles, fd, geo, queue = setup(tmp_path)
    observed = []
    entered = threading.Event()
    finished = threading.Event()

    def ordinary():
        entered.set()

        def publish():
            observed.append(("tail", queue.sq_tail))
            finished.set()

        if synchronize:
            with queue._native_lock:
                publish()
        else:
            publish()

    def firmware():
        until = time.monotonic() + 2
        while int.from_bytes(os.pread(fd, 8, queue.layout.sq_tail), "little") == 0:
            if time.monotonic() > until:
                return
            time.sleep(0.001)
        producer = threading.Thread(target=ordinary)
        producer.start()
        entered.wait(1)
        observed.append(("early", finished.wait(0.02)))
        completion(fd, geo, 1, 0)
        producer.join(1)

    worker = threading.Thread(target=firmware)
    worker.start()
    try:
        native.launch(dev, handles)
        worker.join(3)
        assert observed == [
            ("early", False),
            ("tail", 1),
        ], "another producer entered the detached native exchange"
    finally:
        worker.join(3)
        os.close(fd)
        dev.close()


def test_direct_reserves_queue_until_terminal_ack(tmp_path):
    queue_ownership_witness(tmp_path)
    with pytest.raises(AssertionError, match="another producer"):
        queue_ownership_witness(tmp_path, synchronize=False)


def test_native_queue_ownership_wraps_real_queue_methods():
    dev = D.Device("offline", board="multimesh_v9t6")
    layout = dev.card.queue_layout
    dev.close()
    queue = NodeQueue(HostMemory(), 0, sq_n=4, cq_n=4, layout=layout)
    queue.init()
    lock = queue._native_lock
    entered = threading.Event()
    finished = threading.Event()

    def reset():
        entered.set()
        queue.init()
        finished.set()

    with lock:
        worker = threading.Thread(target=reset)
        worker.start()
        assert entered.wait(1)
        assert not finished.wait(0.02)
    worker.join(1)
    assert finished.is_set()
    # DirectWire requires queue.layout; no .ok() hides a missing layout.
    with pytest.raises(AttributeError):
        _rs.DirectWire(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            0,
            0,
            (4, 4, 0, 0, 0),
            {},
        )
    # If native_direct.attach fails during Device.__init__, the opened card is closed
    # and a close() failure is chained without hiding the attach error.
    closed = []
    with (
        patch.dict(os.environ, {"KTPU_NATIVE_DIRECT": "1"}),
        patch.object(D.OfflineCard, "close", lambda self: closed.append(1) or {}),
        pytest.raises(TypeError, match="KTPU_NATIVE_DIRECT"),
    ):
        D.Device("offline", board="multimesh_v9t6")
    assert closed == [1]

    def failing_close(self):
        raise RuntimeError("card close failed")

    with (
        patch.dict(os.environ, {"KTPU_NATIVE_DIRECT": "1"}),
        patch.object(D.OfflineCard, "close", failing_close),
        pytest.raises(TypeError, match="KTPU_NATIVE_DIRECT") as exc,
    ):
        D.Device("offline", board="multimesh_v9t6")
    assert exc.value.__cause__ is None
    assert any(
        "close failed: RuntimeError('card close failed')" in note
        for note in getattr(exc.value, "__notes__", ())
    )


def test_direct_preserves_stdout_bookkeeping_for_following_normal_run(tmp_path):
    with patch.dict(os.environ, {"KTPU_NATIVE_MEMORY": "1"}):
        dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    layout = dev.card.queue_layout
    path = tmp_path / "queue"
    path.write_bytes(bytes(4 << 20))
    fd = os.open(path, os.O_RDWR)

    class BackedMemory(HostMemory):
        def read_block(self, addr, nbytes):
            return os.pread(fd, nbytes, addr)

        def write_block(self, addr, data):
            os.pwrite(fd, bytes(data), addr)

    mem = BackedMemory()
    queue = NodeQueue(
        mem,
        0,
        sq_n=4,
        cq_n=4,
        so_bytes=4096,
        si_bytes=4096,
        record=layout.record,
        layout=layout,
    )
    geo = queue.geo
    queue.track_ends = True
    queue.init()
    os.pwrite(fd, layout.line_bytes(layout.st_ready), layout.fw)
    transport = SimpleNamespace(
        _c2h=fd, _h2c=fd, fd_generation=1, _native_lock=threading.RLock()
    )
    card = object.__new__(D.SiliconCard)
    card.__dict__.update(dev.card.__dict__)
    card.mem = mem
    card.q = queue
    card.queues = {0: queue}
    card._order, card._stdout, card._segs = {}, {}, {}

    def run_sequence(corrupt_console_at=False):
        queue.init()
        os.pwrite(fd, layout.line_bytes(layout.st_ready), layout.fw)
        direct = _rs.DirectWire(
            queue,
            transport,
            dev.memory,
            0,
            0,
            (4, 4, geo["cq"], geo["rec"], geo["so"]),
            layout.record.fields,
        )
        try:
            card._order.clear()
            card._stdout.clear()
            card._segs.clear()
            direct_out = b"direct run output\n"
            first_tag = queue._tag + 1
            os.pwrite(fd, direct_out, geo["so"])
            os.pwrite(fd, layout.line_bytes(len(direct_out)), layout.so_wr)
            rec1 = bytearray(layout.rec_bytes)
            struct.pack_into("<QQ", rec1, 0, first_tag, len(direct_out))
            os.pwrite(fd, rec1, geo["rec"])
            os.pwrite(fd, struct.pack("<4Q", first_tag, 0, 10, 0), geo["cq"])
            os.pwrite(fd, layout.line_bytes(1), layout.cq_tail)
            os.pwrite(fd, layout.line_bytes(0), layout.sq_head)
            direct.submit(card, bytes(64))
            if corrupt_console_at:
                queue.console_at = queue.so_rd
            assert queue.console_at + len(queue.console) == queue.so_rd
            normal_out = b"normal run output\nPKGS 0 0 7\n"
            total_so = len(direct_out) + len(normal_out)
            tag = card._submit(0, bytes(64), [], admitted=True)
            os.pwrite(fd, normal_out, geo["so"] + len(direct_out))
            os.pwrite(fd, layout.line_bytes(total_so), layout.so_wr)
            rec2 = bytearray(layout.rec_bytes)
            struct.pack_into("<QQ", rec2, 0, tag, total_so)
            os.pwrite(fd, rec2, geo["rec"] + layout.rec_bytes)
            os.pwrite(
                fd, struct.pack("<4Q", tag, 0, 20, 0), geo["cq"] + layout.cq_bytes
            )
            os.pwrite(fd, layout.line_bytes(2), layout.cq_tail)
            os.pwrite(fd, layout.line_bytes(2), layout.sq_head)
            queue.wait(tag)
            assert card._segment(0, tag) == normal_out.decode()
        finally:
            direct.close(shutdown=True)

    try:
        run_sequence(corrupt_console_at=False)
        with pytest.raises(AssertionError):
            run_sequence(corrupt_console_at=True)
    finally:
        os.close(fd)
        dev.close()


def test_direct_close_releases_idle_slot_but_not_uncertain_slot(tmp_path):
    dev, native, handles, fd, geo, _ = setup(tmp_path)
    try:
        completion(fd, geo, 1, 0)
        native.launch(dev, handles)
        endpoint = dev.card.native_direct
        at = endpoint.slot
        endpoint.close()
        assert at not in dev.memory.dram.live and endpoint.slot is None
        with pytest.raises(RuntimeError, match="closed"):
            endpoint.submit(dev.card, bytes(64))
    finally:
        os.close(fd)
        dev.close()


def test_direct_uncertain_owner_cannot_be_recreated(tmp_path):
    dev, native, handles, fd, _, queue = setup(tmp_path)
    queue.timeout = 0.001
    try:
        with pytest.raises(OSError, match="timeout"):
            native.launch(dev, handles)
        endpoint = dev.card.native_direct
        at = endpoint.slot
        with pytest.raises(RuntimeError, match="shutdown"):
            endpoint.close()
        assert at in dev.memory.dram.live
    finally:
        os.close(fd)
        dev.close()


def test_direct_failed_completion_keeps_ownership_until_shutdown(tmp_path):
    dev, native, handles, fd, geo, queue = setup(tmp_path)
    try:
        completion(fd, geo, 1, 0, status=0x53)
        with pytest.raises(RuntimeError, match="completion status"):
            native.launch(dev, handles)
        assert queue._broken and len(dev._uncertain) == 1
        assert any(dev.memory.dram.references.values())
        with pytest.raises(RuntimeError, match="uncertain publication"):
            native.launch(dev, handles)
    finally:
        os.close(fd)
        dev.close()
