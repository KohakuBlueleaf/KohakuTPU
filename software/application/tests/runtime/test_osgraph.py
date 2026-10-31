"""Host witnesses for the restricted resident KDS graph interface."""

import importlib.util
import json
import pathlib
import struct
import subprocess
import sys

import pytest

from kohakutpu.application.runtime import osabi as A
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.application.runtime.oscard import OSCard, movers_enabled
from kohakutpu.application.runtime.osgraph import (
    C_ABI,
    KOS_DE_FETCH_CODE,
    KOS_DE_MAX_ENTRIES,
    KOS_DE_MAX_UNITS,
    KOS_DE_MOVE_CODE,
    KOS_DE_MOVER_SRC,
    KOS_DE_QUEUE_DEPTH,
    KOS_DE_WAIT_CODE,
    MAGIC,
    OSTPU_REQ_EPOCH_WORD,
    Graph,
    ResidentGraph,
    Task,
)

GEN_ABI = pathlib.Path(__file__).resolve().parents[3] / "os/tools/ostpu/gen_abi.py"
SUMMARY_PY = pathlib.Path(__file__).resolve().parents[3] / "os/tools/ostpu/summary.py"
HOSTLIST_PY = pathlib.Path(__file__).resolve().parents[3] / "os/tools/ostpu/hostlist.py"


def package(steps, relocations=()):
    """An independent KAPK wire fixture, one unit, three payload words."""
    raw = bytearray(384)
    struct.pack_into(
        "<12Q",
        raw,
        0,
        96 << 48 | 2 << 32 | 0x4B50414B,
        0,
        len(raw),
        3 | len(relocations) << 32,
        1 | len(steps) << 16 | 1 << 32,
        96,
        128,
        160,
        256,
        288,
        0,
        0,
    )
    struct.pack_into("<QQ", raw, 96, 0x4D47 << 32 | 2 << 16 | 0x0301, 32)
    for i, (op, count, arg) in enumerate(steps):
        struct.pack_into("<QQ", raw, 160 + 16 * i, op | count << 32, arg)
    for i, (bits, addend) in enumerate(relocations):
        struct.pack_into("<Qq", raw, 256 + 16 * i, bits, addend)
    for i in range(3):
        struct.pack_into("<4Q", raw, 288 + 32 * i, i + 11, 22, 33, 44)
    return bytes(raw)


def test_task_wire_offsets_and_split_patch():
    raw = Task(
        1,
        unit=7,
        words=(11, 22, 33, 44),
        pred=(2,),
        patches=((0x123400, 218, 34, 0), (0x123400, 63, 6, 34)),
    ).pack()
    assert len(raw) == 128
    assert raw[:16] == bytes.fromhex("01000102070000000200000000000000")
    assert struct.unpack_from("<4Q", raw, 16) == (11, 22, 33, 44)
    assert raw[48:64] == bytes.fromhex("0034120000000000da00220000000000")
    assert raw[64:80] == bytes.fromhex("00341200000000003f00062200000000")
    assert raw[80:] == bytes(48)


def test_package_wait_patch_and_dependency_mutant(monkeypatch):
    raw = package(
        [(1, 2, 0), (2, 1, 0), (1, 1, 2), (0, 0, 0)], [(218 << 32 | 34 << 40, 64)]
    )

    def witness():
        g = Graph()
        end = g.package(raw, [0x123000], {(2, 1, 3): 7})
        assert g.tasks[0].unit == 7
        assert g.tasks[0].patches == ((0x123040, 218, 34, 0),)
        assert g.tasks[0].pred == g.tasks[1].pred == ()
        third = next(t for t in g.tasks if t.kind == 1 and t.words[0] == 13)
        assert third.pred and g.tasks[third.pred[0]].pred == (0,)
        assert end and g.tasks[end[0]].kind == 0

    witness()
    monkeypatch.setattr(Graph, "join", lambda self, predecessors: ())
    with pytest.raises(AssertionError):
        witness()


def test_unsupported_engine_is_not_silently_dropped():
    with pytest.raises(ValueError, match="unsupported opcode 11"):
        Graph().package(package([(11, 1, 0)]), [], {(2, 1, 3): 7})


def test_await_all_flag_and_removed_flag_mutant():
    def witness(raw):
        g = Graph()
        g.package(raw, [], {(2, 1, 3): 7})
        third = next(t for t in g.tasks if t.kind == 1 and t.words[0] == 13)
        assert third.pred and g.tasks[third.pred[0]].pred == (0, 1)

    witness(package([(1, 2, 0), (2 | 2 << 8, 0, 0), (1, 1, 2)]))
    with pytest.raises(AssertionError):
        witness(package([(1, 2, 0), (2, 0, 0), (1, 1, 2)]))


def test_de_stream_packs_deduplicated_mailbox_mover_and_waits():
    raw = bytearray(package([(1, 2, 0), (4, 1, 2), (3, 0, 0)]))
    struct.pack_into("<4Q", raw, 288 + 64, 0x40, 0x12345678, 0, 1 << 16 | 4)
    g = Graph(batch=32, compact=True, movers=True, de_stream=True)
    end = g.package(raw, [], {(2, 1, 3): 0})
    assert end == (0,) and len(g.tasks) == 1 and g.tasks[0].kind == A.KDS_DE_STREAM
    count = g.tasks[0].words[1]
    codes, vals = [], []
    for k in range((count + 2) // 3):
        c, v1, v2, v3 = struct.unpack_from("<4Q", g.streams[0], 32 * k)
        for j, v in enumerate((v1, v2, v3)):
            if len(codes) < count:
                codes.append((c >> (8 * j)) & 0xFF)
                vals.append(v)
    assert codes[:8] == [0, 1, 2, 3, 4, 5, 1, 5]
    assert 32 in codes
    old = Graph(batch=32, compact=True, movers=True, de_stream=False)
    old.package(raw, [], {(2, 1, 3): 0})
    assert len(old.tasks) > 1 and old.tasks[0].kind != A.KDS_DE_STREAM


def test_stamp_records_are_aligned_and_order_package_work():
    g = Graph(batch=8, compact=True)
    begin = g.stamp((), 40)
    frontier = g.package(package([(1, 1, 0)]), [], {(2, 1, 3): 0}, after=begin)
    end = g.stamp(frontier, 41)
    assert g.tasks[1].pred == begin
    assert g.tasks[end[0]].pred == frontier
    encoded = g.pack(0x2000)
    first = struct.unpack_from("<2Q", encoded, 16)
    last = struct.unpack_from("<2Q", encoded, end[0] * 128 + 16)
    assert first == (0x2000 + len(g.tasks) * 128, 40)
    assert last[0] % 32 == 0 and last[0] > first[0] and last[1] == 41
    mutant = Graph(batch=8, compact=True)
    mutant.stamp((), 40)
    mutant.package(package([(1, 1, 0)]), [], {(2, 1, 3): 0})
    assert mutant.tasks[1].pred != begin


def test_credit_sized_segments_keep_eight_word_syscall_independent():
    raw = bytearray(package([(1, 20, 0), (3, 0, 0)]))
    raw.extend(bytes(288 + 20 * 32 - len(raw)))
    struct.pack_into("<Q", raw, 16, len(raw))
    struct.pack_into("<Q", raw, 24, 20)
    g = Graph(batch=32, compact=True)
    g.package(raw, [], {(2, 1, 3): 0})
    assert [t.words[1] for t in g.tasks if t.kind == 4] == [20]
    old = Graph(batch=8, compact=True)
    old.package(raw, [], {(2, 1, 3): 0})
    assert [t.words[1] for t in old.tasks if t.kind == 4] == [8, 8, 4]
    assert g.streams[0] == raw[288:]


def test_join_pruning_keeps_independent_dependencies():
    graphs = [Graph(compact=True, prune=mode) for mode in (False, True)]
    for g in graphs:
        g.add(Task(0))
        g.add(Task(0, pred=(0,)))
        g.add(Task(0))
        final = g.join((0, 1, 2))
        assert final == (3,)
    assert graphs[0].tasks[-1].pred == (0, 1, 2)
    assert graphs[1].tasks[-1].pred == (1, 2)
    with pytest.raises(AssertionError):
        assert graphs[0].tasks[-1].pred == (1, 2)


def test_compact_frontier_is_not_joined_twice():
    raw = package([(1, 3, 0), (3, 0, 0)])
    small, old = Graph(compact=True), Graph()
    frontier = small.package(raw, [], {(2, 1, 3): 0})
    old.package(raw, [], {(2, 1, 3): 0})
    assert len(small.tasks) == 4 and frontier == (3,)
    assert len(old.tasks) == 5
    with pytest.raises(AssertionError):
        assert len(old.tasks) == 4


def test_segments_split_at_partial_await_and_keep_payload_bytes():
    raw = package([(1, 3, 0), (2, 1, 0), (3, 0, 0)])
    g = Graph(batch=8, compact=True)
    g.package(raw, [], {(2, 1, 3): 0})
    streams = [t for t in g.tasks if t.kind == 4]
    assert [t.words[1] for t in streams] == [1, 2]
    assert b"".join(g.streams.values()) == raw[288:384]
    encoded = g.pack(0x1000)
    assert struct.unpack_from("<Q", encoded, 16)[0] == 0x1000 + len(g.tasks) * 128
    assert encoded[len(g.tasks) * 128 :] == raw[288:384]
    removed_wait = Graph(batch=8, compact=True)
    removed_wait.package(package([(1, 3, 0), (2, 0, 0), (3, 0, 0)]), [], {(2, 1, 3): 0})
    with pytest.raises(AssertionError):
        assert [t.words[1] for t in removed_wait.tasks if t.kind == 4] == [1, 2]


def test_mover_commands_gate_unit_work_and_mwait_is_absolute():
    raw = bytearray(package([(4, 1, 0), (10, 1, 0), (10, 1, 0), (1, 1, 1)]))
    struct.pack_into("<4Q", raw, 288, 0x40, 0x12345678, 0, 1 << 16 | 4)
    graph = Graph(batch=8, compact=True, movers=True)
    graph.package(raw, [], {(2, 1, 3): 0})
    assert [t.kind for t in graph.tasks] == [A.KDS_DE_STREAM, A.KDS_STREAM, A.KDS_NOP]
    assert graph.tasks[1].pred == (0,)
    assert graph.tasks[0].words[:3] == (0, 3, 0)
    assert graph.streams[0] == struct.pack(
        "<4Q",
        A.KOS_DE_MV_BASE + 8 | A.KOS_DE_MV_CTRL << 8 | A.KOS_DE_WAIT_CODE << 16,
        0x12345678,
        1 << 16 | 4,
        A.KOS_DE_MOVER_SRC << 56 | 1,
    )
    struct.pack_into("<Q", raw, 288, 0x44)
    with pytest.raises(ValueError, match="not a mover register"):
        Graph(movers=True).package(raw, [], {(2, 1, 3): 0})
    struct.pack_into("<Q", raw, 288, 0x40)
    with pytest.raises(ValueError, match="unsupported opcode 4"):
        Graph(batch=8, compact=True).package(raw, [], {(2, 1, 3): 0})
    struct.pack_into("<Q", raw, 32, 1 | 1 << 16 | 1 << 32)
    struct.pack_into("<Q", raw, 312, 4)
    with pytest.raises(ValueError, match="incomplete mover"):
        Graph(movers=True).package(raw, [], {(2, 1, 3): 0})


def test_unit_type_binding_rejects_coordinate_collision():
    raw = package([(1, 1, 0)])
    with pytest.raises(ValueError, match="unit type differs"):
        Graph().package(raw, [], {(2, 1, 3): 0}, unit_types={(2, 1, 3): 0x5643})
    graph = Graph()
    graph.package(raw, [], {(2, 1, 3): 0})
    assert graph.tasks[0].unit == 0


class Mailbox:
    def __init__(self):
        self.raw = bytearray(8192)
        self.writes = []
        struct.pack_into("<4Q", self.raw, 0, MAGIC, 1024, 4096, 0x12000)
        struct.pack_into("<4Q", self.raw, 96, 0, 0x10004000, 0, 0)
        struct.pack_into("<4Q", self.raw, 160, 0, 8, 4096, 0)

    def read(self, pa, count):
        return bytes(self.raw[pa : pa + count])

    def write(self, pa, data):
        self.raw[pa : pa + len(data)] = data
        self.writes.append((pa, bytes(data)))
        if pa == 32:
            sequence = struct.unpack_from("<Q", data)[0]
            _op, _count, repeats, _timeout = struct.unpack_from("<4Q", self.raw, 64)
            struct.pack_into(
                "<q7Q", self.raw, 128, 0, 0, 0, 100 * repeats, repeats, 8, 4096, repeats
            )
            struct.pack_into("<Q", self.raw, 96, sequence)

    def idle(self):
        raise AssertionError("synchronous mailbox should already have completed")


def test_recorded_constant_write_uses_resident_copy():
    io = Mailbox()
    session = ResidentGraph(io, 0)
    dev = Device("os", board="vlt_v9t6s_1n", os_session=session, os_units={})
    log = Recorded([("write", session.data_pa + 64, b"\x51" * 32)], [], dev)
    dev.prepare_recorded(log)
    graph = log.prepared.graph
    assert graph.tasks[0].kind == 2
    packed = graph.pack(0x2000)
    assert struct.unpack_from("<4Q", packed, 16) == (0x2080, 0x12040, 1, 0)
    assert packed[128:] == b"\x51" * 32
    io.writes.clear()
    dev.replay(log)
    assert [pa for pa, _ in io.writes] == [64, 32]
    log.close()
    dev.close()


def test_repeated_graph_publishes_once_without_reupload():
    io = Mailbox()
    runtime = ResidentGraph(io, 0)
    g = Graph()
    g.add(Task(0))
    runtime.prepare(g)
    io.writes.clear()
    result = runtime.run(repeats=7)
    assert result["submissions"] == 1 and result["completed"] == 7
    assert [pa for pa, _ in io.writes] == [64, 32]
    assert struct.unpack("<4Q", io.writes[0][1])[:3] == (1, 1, 7)
    runtime.close()


def test_terminal_session_cannot_overwrite_resident_memory(monkeypatch):
    io = Mailbox()
    runtime = ResidentGraph(io, 0)
    g = Graph()
    g.add(Task(0))
    runtime.prepare(g)
    runtime.close()
    io.writes.clear()

    def witness():
        with pytest.raises(RuntimeError, match="closed or failed"):
            runtime.prepare(g)
        assert not io.writes

    witness()
    monkeypatch.setattr(runtime, "_active", lambda: None)
    with pytest.raises(pytest.fail.Exception):
        witness()


def test_failed_publication_cannot_reuse_graph(monkeypatch):
    io = Mailbox()
    runtime = ResidentGraph(io, 0)
    g = Graph()
    g.add(Task(0))
    runtime.prepare(g)
    write = io.write

    def fail_publish(pa, data):
        write(pa, data)
        if pa == 32:
            raise OSError("ambiguous publication")

    monkeypatch.setattr(io, "write", fail_publish)
    with pytest.raises(OSError):
        runtime.run()
    io.writes.clear()
    with pytest.raises(RuntimeError, match="closed or failed"):
        runtime.prepare(g)
    assert not io.writes


def test_os_device_replay_reuses_tasks_and_preserves_firmware_choice(monkeypatch):
    admitted = []
    monkeypatch.setattr(B, "admit", lambda raw, machine, node: admitted.append(raw))
    io = Mailbox()
    session = ResidentGraph(io, 0)
    dev = Device(
        "os", board="vlt_v9t6s_1n", os_session=session, os_units={(2, 1, 3): 0}
    )
    dev.card.unit_types = {(2, 1, 3): 0x4D47}
    raw = package([(1, 1, 0), (0, 0, 0)])
    log = Recorded([("run", {0: (raw, ())}), ("run", {0: (raw, ())})], [], dev)
    dev.prepare_recorded(log)
    assert admitted == [raw, raw]
    io.writes.clear()
    dev.replay(log)
    dev.replay(log)
    assert [pa for pa, _ in io.writes] == [64, 32, 64, 32]
    assert dev.card.submissions == 2
    assert dev.card.runs == 4
    admitted.clear()
    monkeypatch.setattr(B, "admit", lambda *args: None)
    dev.card.prepare_recorded(log, 1 << 20)
    with pytest.raises(AssertionError):
        assert admitted == [raw, raw]
    log.close()
    dev.close()
    with pytest.raises(ValueError, match="booted os_session"):
        Device("os", board="vlt_v9t6s_1n")
    ordinary = Device("offline", board="vlt_v9t6s_1n")
    assert ordinary.kind == "offline"
    ordinary.close()


def test_de_stream_packs_command_groups_and_launch_marks_order_work():
    raw = bytearray(package([(4, 1, 0), (10, 1, 0), (1, 1, 1), (3, 0, 0)]))
    struct.pack_into("<4Q", raw, 288, 0x40, 0x12345678, 0, (1 << 16) | 4)
    g = Graph(movers=True, de_stream=True)
    m0 = g.launch_mark((), (1 << 64) - 1)
    s0 = g.package(raw, [], {(2, 1, 3): 0}, after=m0)
    m1 = g.launch_mark(s0, 0)
    assert m0 == (0,) and s0 == (1,) and m1 == (2,)
    assert [t.kind for t in g.tasks] == [
        A.KDS_LAUNCH_MARK,
        A.KDS_DE_STREAM,
        A.KDS_LAUNCH_MARK,
    ]
    assert g.tasks[0].words[0] == (1 << 64) - 1
    assert g.tasks[1].pred == m0 and g.tasks[2].pred == s0
    payload = g.streams[1]
    assert len(payload) % 32 == 0
    first_codes, v0, v1, v2 = struct.unpack_from("<4Q", payload, 0)
    assert [
        first_codes & 0xFF,
        (first_codes >> 8) & 0xFF,
        (first_codes >> 16) & 0xFF,
    ] == [
        16,
        8,
        32,
    ]
    assert (v0, v1, v2) == (0x12345678, (1 << 16) | 4, (16 << 56) | 1)
    mutant = Graph(movers=True, de_stream=True)
    mutant.launch_mark((), 0)
    assert mutant.tasks[0].pred != s0


def test_de_fetch_and_de_fuse_relocate_instruction_and_mover_blobs():
    raw = bytearray(package([(1, 3, 0), (3, 0, 0)]))
    struct.pack_into(
        "<QQ", raw, 96, 0x4D47 << 32 | 2 << 16 | 0x0301, (0x10200 << 32) | 512
    )
    g_fetch = Graph(movers=True, de_stream=True, de_fetch=True, de_fuse=True)
    f0 = g_fetch.move_de(b"\x33" * 64, 0x20001000)
    f1 = g_fetch.package(raw, [], {(2, 1, 3): 0}, after=f0)
    f2 = g_fetch.package(raw, [], {(2, 1, 3): 0}, after=f1)
    assert f0 == f1 == f2 == (0,)
    assert len(g_fetch.tasks) == 1 and g_fetch.tasks[0].kind == A.KDS_DE_STREAM
    count, blob_bytes = g_fetch.tasks[0].words[1], g_fetch.tasks[0].words[2]
    cmd_bytes = A.de_cmd_bytes(count)
    assert blob_bytes == 64 + 96 + 96
    stream = g_fetch.streams[0]
    assert len(stream) == cmd_bytes + blob_bytes
    assert stream[cmd_bytes : cmd_bytes + 64] == b"\x33" * 64
    assert stream[cmd_bytes + 64 : cmd_bytes + 160] == raw[288:384]
    assert stream[cmd_bytes + 160 :] == raw[288:384]
    codes, vals = [], []
    for k in range((count + 2) // 3):
        c, v1, v2, v3 = struct.unpack_from("<4Q", stream, 32 * k)
        for j, v in enumerate((v1, v2, v3)):
            if len(codes) < count:
                codes.append((c >> (8 * j)) & 0xFF)
                vals.append(v)
    assert codes[0] == (10 | 0x80)
    assert vals[0] == ((1 << 44) | (cmd_bytes << 4) | 0)
    rel_fetches = [(c, v) for c, v in zip(codes, vals, strict=True) if c == (4 | 0x40)]
    assert len(rel_fetches) == 2
    assert rel_fetches[0][1] == (((cmd_bytes + 64) << 24) | (0x60 << 8) | 3)
    assert rel_fetches[1][1] == (((cmd_bytes + 160) << 24) | (0x60 << 8) | 3)
    g_nofetch = Graph(movers=True, de_stream=True, de_fetch=False, de_fuse=True)
    g_nofetch.package(raw, [], {(2, 1, 3): 0})
    assert g_nofetch.tasks[0].words[2] == 0
    g_nofuse = Graph(movers=True, de_stream=True, de_fetch=True, de_fuse=False)
    s = g_nofuse.package(raw, [], {(2, 1, 3): 0})
    g_nofuse.package(raw, [], {(2, 1, 3): 0}, after=s)
    assert len(g_nofuse.tasks) == 2


def test_multinode_os_device_recording_partitions_across_unit_nodes(monkeypatch):
    monkeypatch.setattr(B, "admit", lambda *args: None)
    io = Mailbox()
    session = ResidentGraph(io, 0)
    units = {(0, 1, 1): 0, (1, 1, 1): 1}
    dev = Device("os", board="vlt_v9t6s_1n", os_session=session, os_units=units)
    dev.card.unit_types = {(0, 1, 1): 0x4D47, (1, 1, 1): 0x4D47}
    raw0 = bytearray(package([(1, 1, 0), (0, 0, 0)]))
    raw1 = bytearray(package([(1, 1, 0), (0, 0, 0)]))
    struct.pack_into("<QQ", raw0, 96, 0x4D47 << 32 | 0 << 16 | 0x0101, 32)
    struct.pack_into("<QQ", raw1, 96, 0x4D47 << 32 | 1 << 16 | 0x0101, 32)
    log = Recorded(
        [("run", {0: (bytes(raw0), ()), 1: (bytes(raw1), ())})],
        [],
        dev,
    )
    dev.prepare_recorded(log)
    unit_tasks = [t for t in log.prepared.graph.tasks if t.kind == 1]
    assert [t.unit for t in unit_tasks] == [0, 1]
    log.close()
    dev.close()


def test_os_recording_limit_is_the_task_region_or_the_given_bytes():
    io = Mailbox()
    session = ResidentGraph(io, 0)
    dev = Device("os", board="vlt_v9t6s_1n", os_session=session, os_units={})
    log = Recorded([("write", session.data_pa + 64, b"\x51" * 32)], [], dev)
    # Nothing streams from a resident graph, so the pin fraction never binds.
    dev.card.prepare_recorded(log, None, fraction=0.0)
    with pytest.raises(OutOfMemory, match="resident byte budget"):
        dev.card.prepare_recorded(log, 1)
    dev.card.prepare_recorded(log, 1, overcommit=True)
    log.close()
    dev.close()


def test_c_python_abi_constants_and_drift_mutant(monkeypatch):
    subprocess.run([sys.executable, str(GEN_ABI), "--check"], check=True)
    assert (
        KOS_DE_QUEUE_DEPTH == A.KOS_DE_QUEUE_DEPTH
        and KOS_DE_MAX_ENTRIES == A.KOS_DE_MAX_ENTRIES
        and KOS_DE_FETCH_CODE == A.KOS_DE_FETCH_CODE
        and KOS_DE_MOVE_CODE == A.KOS_DE_MOVE_CODE
        and KOS_DE_WAIT_CODE == A.KOS_DE_WAIT_CODE
        and KOS_DE_MOVER_SRC == A.KOS_DE_MOVER_SRC
        and KOS_DE_MAX_UNITS == A.KOS_DE_MAX_UNITS
        and OSTPU_REQ_EPOCH_WORD == A.OSTPU_REQ_EPOCH_WORD
        and C_ABI["KDS_LZ_USER"] == A.KDS_LZ_USER
        and A.OSTPU_OFF_TRACE == 0x400
        and A.OSTPU_TRACE_WORDS == 5
        and A.OSTPU_OFF_PROF == 0x500
        and A.OSTPU_PROF_WORDS == 8
    )
    raw = bytearray(package([(1, 2, 0), (3, 0, 0)]))
    struct.pack_into(
        "<QQ", raw, 96, 0x4D47 << 32 | 2 << 16 | 0x0301, (0x10200 << 32) | 512
    )

    def build_de_payload():
        g = Graph(movers=True, de_stream=True, de_fetch=True, de_fuse=True)
        f0 = g.move_de(b"\x55" * 32, 0x20001000)
        g.package(raw, [], {(2, 1, 3): 0}, after=f0)
        return g.pack(0x4000)

    baseline = build_de_payload()
    for attr, drifted_val in (
        ("KOS_DE_FETCH_SHIFT", 16),
        ("KOS_DE_MOVE_SHIFT", 8),
        ("KOS_DE_FETCH_CODE", 4 | 0x20),
        ("KOS_DE_MOVE_CODE", 10 | 0x10),
        ("KOS_DE_FETCH_TYPED", 2 << 24),
        ("KOS_DE_FETCH_REPLY", 1 << 29),
        ("KOS_DE_FETCH_COORD_SHIFT", 36),
        ("KOS_DE_FETCH_TAG", 0x50 << 8),
        ("KOS_MV_HDR_VALID", 1 << 43),
        ("KOS_MV_DIM_STRIDE", 16 << 20),
        ("KOS_MV_CTRL_1D", 0x10008),
        ("KDS_TASK_OFF_WORDS", 24),
        ("KOS_DE_MB_GO", 6),
    ):
        with monkeypatch.context() as m:
            m.setattr(A, attr, drifted_val)
            assert build_de_payload() != baseline, f"consumer ignored {attr} drift"

    with monkeypatch.context() as m:
        m.setattr(A, "PKG_HDR_FMT", "<11Q")
        with pytest.raises((IndexError, ValueError)):
            build_de_payload()

    spec_gen = importlib.util.spec_from_file_location("ostpu_gen_abi", GEN_ABI)
    gen_mod = importlib.util.module_from_spec(spec_gen)
    spec_gen.loader.exec_module(gen_mod)
    interp_bad = GEN_ABI.parents[3] / "firmware/kohakuaccel/package/interp.c"
    bad_text = interp_bad.read_text(encoding="utf-8").replace("<< 24", "<< 20", 1)
    bad_path = GEN_ABI.parent / "_tmp_bad_interp.c"
    try:
        bad_path.write_text(bad_text, encoding="utf-8")
        with pytest.raises(ValueError, match="drifted from abi.h"):
            gen_mod.load_from_headers(interp_path=bad_path)
    finally:
        bad_path.unlink(missing_ok=True)

    io = Mailbox()
    session = ResidentGraph(io, 0)
    g = Graph()
    g.add(Task(0))
    session.prepare(g)
    with monkeypatch.context() as m:
        m.setattr(A, "OSTPU_REQ_EPOCH_WORD", 2)
        io.writes.clear()
        session.run()
        req_words = struct.unpack("<4Q", io.writes[1][1])
        assert req_words[1] == 0 and req_words[2] == session.graph_epoch


def test_repeat_opcode_unrolls_in_both_stream_and_de_stream_modes():
    raw = bytearray(package([(9, 1, (1 << 48) | (3 << 32) | 0), (3, 0, 0)]))
    struct.pack_into("<4Q", raw, 288, 100, 22, 33, 44)
    struct.pack_into("<2Q", raw, 288 + 32, (16 << 40) | (0 << 32) | 0, 5)
    g_stream = Graph(batch=8, compact=True)
    g_stream.package(raw, [], {(2, 1, 3): 0})
    words_stream = [
        struct.unpack_from("<Q", g_stream.streams[0], 32 * k)[0] for k in range(3)
    ]
    assert words_stream == [100, 105, 110]
    g_de = Graph(de_stream=True, de_fetch=False)
    g_de.package(raw, [], {(2, 1, 3): 0})
    vals_de = []
    stream_de = g_de.streams[0]
    for k in range(len(stream_de) // 32):
        c, v0, v1, v2 = struct.unpack_from("<4Q", stream_de, 32 * k)
        for j, v in enumerate((v0, v1, v2)):
            if ((c >> (8 * j)) & 0xFF) == 1:
                vals_de.append(v)
    assert vals_de == [100, 105, 110]
    with pytest.raises(AssertionError):
        assert [100, 100, 100] == words_stream


def test_immediate_load_alu_and_assert_zero_contract_witness():
    raw_ok = bytearray(
        package(
            [
                (15 | (2 << 8) | (1 << 16), 0, 0),
                (16 | (2 << 8) | (2 << 16), 4 | (1 << 8), 31),
                (22 | (2 << 16), 7, 0),
                (1, 1, 1),
            ]
        )
    )
    struct.pack_into("<4Q", raw_ok, 288, 0x20001000, 0, 0, 0)
    g = Graph(de_stream=True)
    g.package(raw_ok, [], {(2, 1, 3): 0})
    assert len(g.tasks) == 1
    raw_bad = bytearray(raw_ok)
    struct.pack_into("<4Q", raw_bad, 288, 0x20001004, 0, 0, 0)
    with pytest.raises(ValueError, match="package contract 7 failed"):
        Graph(de_stream=True).package(raw_bad, [], {(2, 1, 3): 0})
    with pytest.raises(ValueError, match="supports only immediate"):
        Graph(de_stream=True).package(
            package([(15 | (16 << 8), 0, 0)]), [], {(2, 1, 3): 0}
        )
    raw_mem = bytearray(package([(15 | (14 << 16), 0, 0), (22 | (14 << 16), 1, 0)]))
    struct.pack_into("<4Q", raw_mem, 288, 0x20002000, 0, 0, 0)
    gm = Graph(de_stream=True)
    gm.package(raw_mem, [], {(2, 1, 3): 0})
    c0, v0, _, _ = struct.unpack_from("<4Q", gm.streams[0], 0)
    assert (c0 & 0xFF) == C_ABI["KOS_DE_CHECK_ZERO"] and v0 == 0x20002000


def load_mutated_osgraph(tmp_path, old, new):
    osgraph_path = pathlib.Path(sys.modules[Graph.__module__].__file__)
    osgraph_src = osgraph_path.read_text(encoding="utf-8")
    mut_src = osgraph_src.replace(old, new, 1)
    assert mut_src != osgraph_src
    mut_file = tmp_path / "osgraph_mut.py"
    mut_file.write_text(mut_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("osgraph_mut", mut_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_single_unfused_package_splits_at_max_entries_and_unit_limit(
    monkeypatch, tmp_path
):
    raw = bytearray(package([(1, 3, 0), (2, 2, 0), (1, 3, 0), (3, 0, 0)]))
    with pytest.raises(ValueError, match="unit index exceeds DE counter capacity"):
        Graph(de_stream=True).package(raw, [], {(2, 1, 3): A.KOS_DE_MAX_UNITS})

    def witness(graph_cls):
        with monkeypatch.context() as m:
            m.setattr(A, "KOS_DE_MAX_ENTRIES", 16)
            g = graph_cls(de_stream=True, de_fetch=False, de_fuse=False)
            end = g.package(raw, [], {(2, 1, 3): 0})
            assert len(g.tasks) > 1 and end == (len(g.tasks) - 1,)
            assert all(t.kind == A.KDS_DE_STREAM for t in g.tasks)
            assert all(0 < t.words[1] <= 16 for t in g.tasks)
            assert [g.tasks[i].pred for i in range(1, len(g.tasks))] == [
                (i - 1,) for i in range(1, len(g.tasks))
            ]

    witness(Graph)
    mut_mod = load_mutated_osgraph(
        tmp_path,
        "len(entries) + needed + reserve <= A.KOS_DE_MAX_ENTRIES",
        "True",
    )
    with pytest.raises(AssertionError):
        witness(mut_mod.Graph)


def test_reattached_resident_graph_generates_unique_epoch_and_continues_sequence(
    tmp_path,
):
    def witness(rg_cls):
        io = Mailbox()
        s1 = rg_cls(io, 0)
        g1 = Graph()
        g1.add(Task(0, words=(1, 0, 0, 0)))
        s1.prepare(g1)
        s1.run()
        assert s1.sequence == 1

        s2 = rg_cls(io, 0)
        assert s2.sequence == 1
        g2 = Graph()
        g2.add(Task(0, words=(2, 0, 0, 0)))
        s2.prepare(g2)
        assert s2.graph_epoch != s1.graph_epoch
        s2.run()
        assert s2.sequence == 2

    witness(ResidentGraph)
    mut_mod = load_mutated_osgraph(
        tmp_path,
        "(digest ^ ((self.sequence + 1) << 16) ^ self._prep_nonce) & ((1 << 64) - 1)",
        "self._prep_nonce",
    )
    with pytest.raises(AssertionError):
        witness(mut_mod.ResidentGraph)


def test_explicit_baseline_refuses_missing_hostl_and_summary_verifies_outputs():
    spec_hl = importlib.util.spec_from_file_location("ostpu_hostlist", HOSTLIST_PY)
    hl_mod = importlib.util.module_from_spec(spec_hl)
    spec_hl.loader.exec_module(hl_mod)
    with pytest.raises(ValueError, match="lists must be 0"):
        hl_mod.run_firmware_capture({}, lists="auto")

    spec_sum = importlib.util.spec_from_file_location("ostpu_summary", SUMMARY_PY)
    sum_mod = importlib.util.module_from_spec(spec_sum)
    spec_sum.loader.exec_module(sum_mod)
    fp16_a = struct.pack("<4e", 1.0, 2.0, 3.0, 4.0).hex()
    fp16_b = struct.pack("<4e", 1.5, 2.5, 3.5, 4.5).hex()
    good = dict(
        workload="vit-qkv-warmjit",
        backend="os",
        board="card_v9t6s_1n",
        results=[
            dict(cycles=100, outputs=[fp16_a]),
            dict(cycles=80, outputs=[fp16_b]),
        ],
    )
    out = sum_mod.summarize_results([good], reference=good, check_sensitivity=True)
    assert out["bit_exact"] is True and out["entries"][0]["changed_elements"] == 4
    bad_ref = dict(
        good,
        results=[dict(cycles=100, outputs=[fp16_b]), dict(cycles=80, outputs=[fp16_b])],
    )
    with pytest.raises(ValueError, match="--ref is required"):
        sum_mod.summarize_results([good])
    with pytest.raises(ValueError, match="outputs differ"):
        sum_mod.summarize_results([good], reference=bad_ref)
    with pytest.raises(ValueError, match="did not change"):
        sum_mod.summarize_results([bad_ref], reference=bad_ref, check_sensitivity=True)


def test_os_device_prepares_through_the_hoste_card_contract(monkeypatch):
    """Device.prepare_recorded calls the card as HOSTE's NodeCard is called.
    A resident OS graph pins the whole recording in the 8-task resident
    region and streams nothing: the pin fraction does not bind, the byte
    budget does (overcommit lifts it to the region), and execution lists are
    refused."""
    monkeypatch.setattr(B, "admit", lambda *args: None)
    raw = package([(1, 1, 0), (0, 0, 0)])
    region = 8 * A.OSTPU_TASK_BYTES

    def prepared(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        session = ResidentGraph(Mailbox(), 0)
        dev = Device(
            "os", board="vlt_v9t6s_1n", os_session=session, os_units={(2, 1, 3): 0}
        )
        dev.card.unit_types = {(2, 1, 3): 0x4D47}
        log = Recorded([("run", {0: (raw, ())})], [], dev)
        try:
            dev.prepare_recorded(log)
            dev.replay(log)
            graph = log.prepared.graph
            return len(graph.tasks) * A.OSTPU_TASK_BYTES + sum(
                map(len, graph.streams.values())
            )
        finally:
            log.close()
            dev.close()
            for name in env:
                monkeypatch.delenv(name)

    size = prepared()
    assert 0 < size <= region
    assert prepared(KOHAKUTPU_REPLAY_PACKAGE_FRACTION="0") == size
    assert prepared(KOHAKUTPU_REPLAY_PACKAGE_BYTES=str(size)) == size
    with pytest.raises(OutOfMemory, match="resident byte budget"):
        prepared(KOHAKUTPU_REPLAY_PACKAGE_BYTES=str(size - 1))
    assert prepared(
        KOHAKUTPU_REPLAY_PACKAGE_BYTES=str(size - 1), KOHAKUTPU_REPLAY_OVERCOMMIT="1"
    )
    with pytest.raises(ValueError, match="execution lists"):
        prepared(KOHAKUTPU_EXECUTION_LIST="1")
    monkeypatch.setattr(OSCard, "prepare_recorded", lambda self, log, budget: None)
    with pytest.raises(TypeError):
        prepared()


def test_movers_resolve_per_machine(monkeypatch):
    """KOHAKUTPU_OS_MOVERS=1 lowers mover steps only where the machine has the
    dispatch engine that carries them; elsewhere it is MOVERS=0."""
    dev = Device(
        "os",
        board="vlt_v9t6s_1n",
        os_session=ResidentGraph(Mailbox(), 0),
        os_units={(2, 1, 3): 0},
    )
    with_engine = dev.card.machine_json
    dev.close()
    doc = json.loads(with_engine)
    assert doc["machine"]["node"]["engine"]["depth"] > 0
    del doc["machine"]["node"]["engine"]
    without = json.dumps(doc)
    monkeypatch.setenv("KOHAKUTPU_OS_MOVERS", "1")
    assert movers_enabled(with_engine) and not movers_enabled(without)
    monkeypatch.setenv("KOHAKUTPU_OS_MOVERS", "0")
    assert not movers_enabled(with_engine) and not movers_enabled(without)
