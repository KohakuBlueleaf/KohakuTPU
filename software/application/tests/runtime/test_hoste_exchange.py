"""Native endpoint witnesses over a shared file, not FPGA/XDMA measurements."""

import os
import struct
import threading
import time
from pathlib import Path

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakutpu.compiler import _rs

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="native pread/pwrite endpoint"
)

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)


def fixture(tmp_path):
    path = tmp_path / "queue"
    path.write_bytes(bytes(65536))
    fd = os.open(path, os.O_RDWR)
    geo = LAYOUT.geometry(4, 4, 4096, 4096)
    os.pwrite(fd, LAYOUT.info_line(LAYOUT.version, 4, 4, 4096, 4096, 1), LAYOUT.info)
    os.pwrite(fd, LAYOUT.line_bytes(LAYOUT.st_ready), LAYOUT.fw)
    endpoint = _rs.QueueExchange(fd, fd, LAYOUT)
    return fd, endpoint, (4, 4, geo["cq"], geo["rec"], geo["so"]), geo


def serve(fd, geo, epoch=1):
    end = time.monotonic() + 3
    while int.from_bytes(os.pread(fd, 8, LAYOUT.sq_tail), "little") == 0:
        if time.monotonic() > end:
            return
        time.sleep(0.0001)
    entry = struct.unpack("<8Q", os.pread(fd, LAYOUT.sq_bytes, LAYOUT.sq))
    os.pwrite(fd, b"native stdout", geo["so"])
    os.pwrite(fd, LAYOUT.line_bytes(13), LAYOUT.so_wr)
    record = bytearray(LAYOUT.rec_bytes)
    struct.pack_into("<QQ", record, LAYOUT.record.fields["tag"][0], entry[1], 13)
    struct.pack_into(
        "<QQQ",
        record,
        LAYOUT.record.fields["list_index"][0],
        2,
        19,
        epoch,
    )
    os.pwrite(fd, record, geo["rec"])
    os.pwrite(fd, struct.pack("<4Q", entry[1], 0, 12345, 2), geo["cq"])
    os.pwrite(fd, LAYOUT.line_bytes(1), LAYOUT.sq_head)
    os.pwrite(fd, LAYOUT.line_bytes(1), LAYOUT.cq_tail)


def test_native_exchange_publishes_list_and_collects_without_callbacks(tmp_path):
    fd, endpoint, geometry, geo = fixture(tmp_path)
    worker = threading.Thread(target=serve, args=(fd, geo))
    worker.start()
    try:
        got = endpoint.run(0, geometry, 0, 0, 0, LAYOUT.list_entry(7, 8192, 2, 1), 2.0)
        head, stdout, completion, record, console, polls, published, error = got
        assert error is None and published and head == 1
        assert stdout == 13 and console == b"native stdout" and polls >= 2
        assert struct.unpack("<4Q", completion) == (7, 0, 12345, 2)
        assert LAYOUT.record.decode(record)["list_index"] == 2
        assert int.from_bytes(os.pread(fd, 8, LAYOUT.cq_head), "little") == 1
    finally:
        worker.join()
        os.close(fd)


def test_native_exchange_refuses_stale_completion_without_ack(tmp_path):
    fd, endpoint, geometry, geo = fixture(tmp_path)
    worker = threading.Thread(target=serve, args=(fd, geo, 9))
    worker.start()
    try:
        got = endpoint.run(0, geometry, 0, 0, 0, LAYOUT.list_entry(7, 8192, 2, 1), 2.0)
        assert got[-1] == "native completion epoch mismatch"
        assert got[-2] and got[0] == 0
        assert os.pread(fd, 8, LAYOUT.cq_head) == bytes(8)
    finally:
        worker.join()
        os.close(fd)

    # RUN entries check entry[run_tag], not entry[list_tag], when list_tag != run_tag.
    drifted_schema = dict(LAYOUT.schema)
    drifted_schema["list_words"] = {**LAYOUT.list_words, "tag": 5}
    drifted_layout = L.Layout.from_schema(drifted_schema)
    path2 = tmp_path / "queue_run_tag"
    path2.write_bytes(bytes(65536))
    fd2 = os.open(path2, os.O_RDWR)
    os.pwrite(fd2, LAYOUT.info_line(LAYOUT.version, 4, 4, 4096, 4096, 1), LAYOUT.info)
    os.pwrite(fd2, LAYOUT.line_bytes(LAYOUT.st_ready), LAYOUT.fw)
    endpoint2 = _rs.QueueExchange(fd2, fd2, drifted_layout)
    worker2 = threading.Thread(target=serve, args=(fd2, geo, 1))
    worker2.start()
    try:
        got2 = endpoint2.run(
            0, geometry, 0, 0, 0, drifted_layout.run_entry(7, 8192, 32), 2.0
        )
        assert got2[-1] is None and got2[0] == 1
        bad_entry = list(drifted_layout.run_entry(7, 8192, 32))
        bad_entry[drifted_layout.run_tag] = 99
        os.pwrite(fd2, LAYOUT.line_bytes(0), LAYOUT.sq_tail)
        os.pwrite(fd2, LAYOUT.line_bytes(0), LAYOUT.sq_head)
        os.pwrite(fd2, LAYOUT.line_bytes(0), LAYOUT.cq_tail)
        os.pwrite(fd2, LAYOUT.line_bytes(0), LAYOUT.cq_head)
        os.pwrite(fd2, LAYOUT.line_bytes(0), LAYOUT.so_wr)
        os.pwrite(fd2, LAYOUT.line_bytes(0), LAYOUT.so_rd)
        os.pwrite(fd2, struct.pack("<4Q", 7, 0, 12345, 2), geo["cq"])
        rec = bytearray(LAYOUT.rec_bytes)
        struct.pack_into("<QQ", rec, LAYOUT.record.fields["tag"][0], 7, 0)
        os.pwrite(fd2, rec, geo["rec"])
        os.pwrite(fd2, LAYOUT.line_bytes(1), LAYOUT.cq_tail)
        mismatch = endpoint2.run(0, geometry, 0, 0, 0, bad_entry, 0.05)
        assert mismatch[-1] == "native completion tag mismatch"
    finally:
        worker2.join()
        os.close(fd2)


def test_native_exchange_timeout_keeps_publication_uncertain(tmp_path):
    fd, endpoint, geometry, _ = fixture(tmp_path)
    try:
        got = endpoint.run(0, geometry, 0, 0, 0, LAYOUT.run_entry(7, 8192, 32), 0.001)
        assert got[-1] == "native CQ timeout" and got[-2]
        assert got[0] == 0 and not got[2]
    finally:
        os.close(fd)
