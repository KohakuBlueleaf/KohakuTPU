"""The host side of the node queue, against a firmware written from the spec.

`SpecFirmware` (kohakuaccel.driver.node.queue.double) implements
docs/spec/node-queue.md's firmware half independently of `NodeQueue`, over an
in-memory card; the queue is driven through it as it would be through a node.
The layout constants are compared with the firmware's C header, which nothing
else ties them to.
"""

import pathlib
import re
import struct

import pytest
from kohakuaccel.compiler.package.build import PackageBuilder
from kohakuaccel.compiler.package.format import Package
from kohakuaccel.driver.node.boot import MAX_UNITS, BootArgs, unit_word
from kohakuaccel.driver.node.boot import args as boot_args
from kohakuaccel.driver.node.queue import NodeError, NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakuaccel.simulation.package import Interpreter

SOFTWARE = pathlib.Path(__file__).resolve().parents[2]
INC = SOFTWARE / "firmware/kohakuaccel/include/ka"
BASE = 0x10_0000


def c_defines(path: pathlib.Path) -> dict:
    text = path.read_text()
    out = {}
    for name, val in re.findall(r"#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)", text):
        out[name] = int(val, 0)
    for name, val in re.findall(r"\b(KA_\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*,", text):
        out[name] = int(val, 0)
    return out


def test_queue_layout_matches_the_firmware():
    c = c_defines(INC / "queue/layout.h")
    assert c["KA_Q_MAGIC"] == L.MAGIC and c["KA_Q_VERSION"] == L.VERSION
    for name in (
        "INFO",
        "FW",
        "SQ_TAIL",
        "SQ_HEAD",
        "CQ_TAIL",
        "CQ_HEAD",
        "SO_WR",
        "SO_RD",
        "SI_WR",
        "SI_RD",
        "UNITS",
        "HEAPS",
        "SQ",
    ):
        assert c[f"KA_Q_{name}"] == getattr(L, name), name
    assert c["KA_Q_SQ_BYTES"] == L.SQ_BYTES and c["KA_Q_CQ_BYTES"] == L.CQ_BYTES
    for name in ("NOP", "RUN", "STOP", "HEAP", "ALLOC", "FREE"):
        assert c[f"KA_SQ_{name}"] == getattr(L, f"OP_{name}"), name
    s = c_defines(INC / "package/status.h")
    for code, name in L.STATUS.items():
        assert s[f"KA_ST_{name}"] == code, name
    m = c_defines(INC / "os/mem.h")
    assert m["KA_MEM_REGIONS"] == L.MEM_REGIONS and m["KA_MEM_DRAM"] == L.MEM_DRAM
    # A full unit table (count line, then MAX_UNITS words) ends before the heap
    # lines, which end before the SQ.
    assert L.UNITS + L.LINE + 8 * MAX_UNITS <= L.HEAPS
    assert L.HEAPS + L.MEM_REGIONS * L.LINE <= L.SQ


def test_boot_block_matches_the_firmware():
    c = c_defines(INC / "boot/args.h")
    assert c["KA_BOOT_MAGIC"] == boot_args.MAGIC
    assert c["KA_BOOT_VERSION"] == boot_args.VERSION
    assert c["KA_MAX_UNITS"] == MAX_UNITS
    raw = BootArgs(
        queue=0x1234_5000, mesh=1, scan=3, units=[unit_word(0x4D47, 1, 1, 1)]
    ).pack()
    words = struct.unpack(f"<{len(raw) // 8}Q", raw)
    assert words[:10] == (
        boot_args.MAGIC,
        2,
        0x1234_5000,
        1,
        3,
        2_000_000,
        0,
        0,
        1,
        0x4D47 << 32 | 1 << 16 | 1 << 8 | 1,
    )
    with pytest.raises(ValueError):
        BootArgs(queue=0, units=[0] * (MAX_UNITS + 1)).pack()


class HostMemory(MemoryTransport):
    """Records the HOST's writes, which must be whole 32-byte lines."""

    def __init__(self) -> None:
        super().__init__()
        self.host_writes: list[tuple[int, int]] = []
        self.firmware = False

    def write_block(self, addr, data):
        if not self.firmware:
            self.host_writes.append((addr, len(data)))
        super().write_block(addr, data)


def _queue(**kw):
    mem = HostMemory()
    mailbox = kw.pop("mailbox", None)
    fw = SpecFirmware(mem, BASE, Interpreter(mailbox) if mailbox else None)
    q = NodeQueue(mem, BASE, idle=lambda: fw.step(), **kw)
    q.init()
    fw.boot()
    return mem, fw, q


def test_round_trip_nop_and_package():
    _mem, _fw, q = _queue()
    assert q.wait_ready()["state"] == "ready"
    assert q.nop().ok
    pkg = PackageBuilder().build().to_bytes()
    c = q.run(pkg, bindings=[0x40_0000, 0x50_0000])
    assert c.ok
    assert "run 96B [4194304, 5242880]" in q.read_stdout()


def test_a_package_is_uploaded_once_and_found_by_digest():
    _mem, _fw, q = _queue()
    pkg = Package(payloads=[1, 2, 3]).to_bytes()
    q.run(pkg)
    q.run(pkg, bindings=[7])
    q.run(Package(payloads=[4]).to_bytes())
    assert q.counters["uploads"] == 2 and q.counters["cache_hits"] == 1


def test_entries_and_bindings_land_where_the_spec_says():
    mem, _fw, q = _queue(sq_n=4, cq_n=4)
    pkg = Package(payloads=[9]).to_bytes()
    tag = q.submit(pkg, bindings=[0xAA, 0xBB])
    sq = BASE + L.SQ
    words = [mem.read64(sq + 8 * k) for k in range(8)]
    assert words[0] == 1 | 2 << 16 and words[1] == tag and words[3] == len(pkg)
    assert mem.read_block(words[2], len(pkg)) == pkg
    assert [mem.read64(words[4]), mem.read64(words[4] + 8)] == [0xAA, 0xBB]
    assert mem.read64(BASE + L.SQ_TAIL) == 1
    q.wait(tag)
    assert mem.read64(BASE + L.CQ_HEAD) == 1


def test_a_full_submission_ring_waits_for_the_firmware():
    _mem, _fw, q = _queue(sq_n=2, cq_n=8)
    tags = [q.submit(None, op=L.OP_NOP) for _ in range(5)]
    for t in tags:
        assert q.wait(t).ok


def test_a_failed_package_raises_with_its_status():
    _mem, _fw, q = _queue()
    tag = q.submit(None, op=0x77)
    with pytest.raises(NodeError, match="BAD_OP"):
        q.wait(tag)


def test_node_heap_ops_through_the_queue():
    mem, _fw, q = _queue()
    q.heap(L.MEM_DRAM, 0x40_0000, 1 << 16)
    words = [mem.read64(BASE + L.SQ + 8 * k) for k in range(8)]
    assert words[0] == L.OP_HEAP and words[2:6] == [L.MEM_DRAM, 0x40_0000, 1 << 16, 64]
    a = q.alloc(L.MEM_DRAM, 100, align=256, tag=5)
    b = q.alloc(L.MEM_DRAM, 1)
    assert a == 0x40_0000 and b == 0x40_0080
    st = q.heap_stats(L.MEM_DRAM)
    assert st == {
        "free": (1 << 16) - 192,
        "largest": (1 << 16) - 192,
        "live": 2,
        "blocks": 3,
        "fails": 0,
    }
    with pytest.raises(NodeError, match="HEAP_BUSY"):
        q.heap(L.MEM_DRAM, 0x40_0000, 4096)
    with pytest.raises(NodeError, match="NO_MEMORY"):
        q.alloc(L.MEM_DRAM, 1 << 17)
    q.free(L.MEM_DRAM, a)
    with pytest.raises(NodeError, match="BAD_FREE"):
        q.free(L.MEM_DRAM, a)
    with pytest.raises(NodeError, match="BAD_ARG"):
        q.alloc(L.MEM_STAGING, 64)
    q.free(L.MEM_DRAM, b)
    st = q.heap_stats(L.MEM_DRAM)
    assert (
        st["free"] == st["largest"] == 1 << 16
        and st["blocks"] == 1
        and st["fails"] == 1
    )


def test_stdout_wraps_and_stdin_lands_whole_lines():
    mem, fw, q = _queue(so_bytes=32, si_bytes=64)
    for k in range(3):
        fw.print(f"line {k} of a ring\n")
        assert q.read_stdout() == f"line {k} of a ring\n"
    q.stdin("hello")
    si = BASE + L.SQ + 64 * q.sq_n + 32 * q.cq_n + 32
    assert mem.read_block(si, 8)[:5] == b"hello"
    assert mem.read64(BASE + L.SI_WR) == 5
    assert mem.host_writes and all(
        a % 32 == 0 and n % 32 == 0 for a, n in mem.host_writes
    )
