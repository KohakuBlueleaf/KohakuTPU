"""The host side of the node queue, against a firmware written from the spec.

`SpecFirmware` (kohakuaccel.driver.node.queue.double) implements
docs/spec/node-queue.md's firmware half independently of `NodeQueue`, over an
in-memory card; the queue is driven through it as it would be through a node.
The layout constants are compared with the firmware's C header, which nothing
else ties them to.
"""

import json
import pathlib
import re
import struct

import pytest

from kohakuaccel.driver.node.boot import MAX_UNITS, BootArgs, unit_word
from kohakuaccel.driver.node.boot import args as boot_args
from kohakuaccel.driver.node.queue import (
    BootRefused,
    FirmwareFatal,
    NodeError,
    NodeQueue,
    OverBudget,
)
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import RecordSkew
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakuaccel.simulation.package import Interpreter
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF
from kohakutpu.simulation.package import Reader

SOFTWARE = pathlib.Path(__file__).resolve().parents[2]
INC = SOFTWARE / "firmware/kohakuaccel/include/ka"
BASE = 0x10_0000
LAYOUT = L.Layout.from_machine(MF.MACHINES / "multimesh_v9t6.json")


def package(words=()):
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    return _rs.package_build(
        '{"credit": 32}', json.dumps([["payload", w] for w in words]), text
    )


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
    assert c["KA_Q_MAGIC"] == LAYOUT.magic and c["KA_Q_VERSION"] == LAYOUT.version
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
        "SQ",
    ):
        assert c[f"KA_Q_{name}"] == getattr(LAYOUT, name.lower()), name
    assert (
        c["KA_Q_SQ_BYTES"] == LAYOUT.sq_bytes and c["KA_Q_CQ_BYTES"] == LAYOUT.cq_bytes
    )
    for name in ("NOP", "RUN", "STOP", "LIST"):
        assert c[f"KA_SQ_{name}"] == getattr(LAYOUT, f"op_{name.lower()}"), name
    s = c_defines(INC / "package/status.h")
    for code, name in L.STATUS.items():
        assert s[f"KA_ST_{name}"] == code, name
    # RUL.031: every status and wait the header declares, by its own name.
    assert {v for k, v in s.items() if k.startswith("KA_ST_")} == set(L.STATUS)
    waits = {
        v: k.removeprefix("KA_WAIT_").lower()
        for k, v in s.items()
        if k.startswith("KA_WAIT_")
    }
    assert waits == L.WAITS
    assert LAYOUT.units + LAYOUT.line + 8 * MAX_UNITS <= LAYOUT.sq
    for name in ("HEAP", "ALLOC", "FREE"):
        assert f"KA_SQ_{name}" not in c
        assert name.lower() not in LAYOUT.opcodes
        assert not hasattr(LAYOUT, f"op_{name.lower()}")


def test_boot_block_matches_the_firmware():
    c = c_defines(INC / "boot/args.h")
    assert c["KA_BOOT_MAGIC"] == boot_args.MAGIC
    assert c["KA_BOOT_VERSION"] == boot_args.VERSION
    assert c["KA_MAX_UNITS"] == MAX_UNITS
    assert c["KA_BOOT_REGIONS"] == boot_args.REGIONS
    assert c["KA_REGION_DEBUG"] == boot_args.REGION_DEBUG
    errs = {
        v: k.removeprefix("KA_BOOT_ERR_")
        for k, v in c.items()
        if k.startswith("KA_BOOT_ERR_")
    }
    assert errs == {
        boot_args.ERR_MAGIC: "MAGIC",
        boot_args.ERR_VERSION: "VERSION",
        boot_args.ERR_UNITS: "UNITS",
        boot_args.ERR_REGIONS: "REGIONS",
        boot_args.ERR_QUEUE: "QUEUE",
    }
    assert set(errs) == set(boot_args.REFUSALS)
    raw = BootArgs(
        queue=0x1234_5000,
        mesh=1,
        scan=3,
        timeout=2_000_000,
        units=[unit_word(0x4D47, 1, 1, 1)],
        regions=[(0x8000_0000, 0x1_0000, boot_args.REGION_DEBUG)],
    ).pack()
    words = struct.unpack(f"<{len(raw) // 8}Q", raw)
    # The region table follows the unit table (args.h: units[KA_MAX_UNITS]).
    at = 9 + MAX_UNITS
    assert len(words) == at + 3 * boot_args.REGIONS
    assert words[at : at + 6] == (0x8000_0000, 0x1_0000, 1, 0, 0, 0)
    assert words[:10] == (
        boot_args.MAGIC,
        3,
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
        BootArgs(queue=0, timeout=1, units=[0] * (MAX_UNITS + 1)).pack()
    with pytest.raises(ValueError):
        BootArgs(
            queue=0, timeout=1, regions=[(0, 0, 0)] * (boot_args.REGIONS + 1)
        ).pack()
    # RUL.033: no raw default bound.
    with pytest.raises(TypeError):
        BootArgs(queue=0)


class HostMemory(MemoryTransport):
    """Records the HOST's writes, which must be whole queue lines."""

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
    fw = SpecFirmware(
        mem,
        BASE,
        Interpreter(mailbox, reader=Reader()) if mailbox else None,
        layout=LAYOUT,
    )
    q = NodeQueue(mem, BASE, idle=lambda: fw.step(), layout=LAYOUT, **kw)
    q.init()
    fw.boot()
    return mem, fw, q


def test_round_trip_nop_and_package():
    _mem, _fw, q = _queue()
    assert q.wait_ready()["state"] == "ready"
    assert q.nop().ok
    pkg = package()
    c = q.run(pkg, bindings=[0x40_0000, 0x50_0000])
    assert c.ok
    assert "run 96B [4194304, 5242880]" in q.read_stdout()


def test_a_package_is_uploaded_once_and_found_by_digest():
    _mem, _fw, q = _queue()
    pkg = package([1, 2, 3])
    q.run(pkg)
    q.run(pkg, bindings=[7])
    q.run(package([4]))
    assert q.counters["uploads"] == 3 and q.counters["cache_hits"] == 0


def test_entries_and_bindings_land_where_the_spec_says():
    mem, _fw, q = _queue(sq_n=4, cq_n=4)
    pkg = package([9])
    tag = q.submit(pkg, bindings=[0xAA, 0xBB])
    sq = BASE + LAYOUT.sq
    words = [mem.read64(sq + 8 * k) for k in range(8)]
    assert words[0] == 1 | 2 << 16 and words[1] == tag and words[3] == len(pkg)
    assert mem.read_block(words[2], len(pkg)) == pkg
    assert [mem.read64(words[4]), mem.read64(words[4] + 8)] == [0xAA, 0xBB]
    assert mem.read64(BASE + LAYOUT.sq_tail) == 1
    q.wait(tag)
    assert mem.read64(BASE + LAYOUT.cq_head) == 1


def test_a_full_submission_ring_waits_for_the_firmware():
    _mem, _fw, q = _queue(sq_n=2, cq_n=8)
    tags = [q.submit(None, op=LAYOUT.op_nop) for _ in range(5)]
    for t in tags:
        assert q.wait(t).ok


def test_a_failed_package_raises_with_its_status():
    _mem, _fw, q = _queue()
    tag = q.submit(None, op=0x77)
    with pytest.raises(NodeError, match="BAD_OP"):
        q.wait(tag)


def test_a_wait_ends_when_the_firmware_goes_fatal():
    # DBG.SESS.004: the firmware traps while the host waits; its line reads
    # FATAL with the exit code, and the wait ends at once, not at its timeout.
    mem, _fw, q = _queue()
    q.wait_ready()
    code = 0xE000_0000_0000_0005

    def trap():
        mem.write_block(
            BASE + LAYOUT.fw, struct.pack("<4Q", LAYOUT.st_fatal, 0, 0, code)
        )

    q.idle = trap
    tag = q.submit(None, op=LAYOUT.op_nop)
    with pytest.raises(FirmwareFatal) as e:
        q.wait(tag, timeout=1.0)
    assert e.value.state == {"state": "fatal", "abi": 0, "done": 0, "fatal": code}


def _corrupt_boot_block(word: int, value: int) -> bytes:
    raw = bytearray(BootArgs(queue=BASE, timeout=2_000_000).pack())
    struct.pack_into("<Q", raw, 8 * word, value)
    return bytes(raw)


@pytest.mark.parametrize(
    "raw,code,reason",
    [
        (_corrupt_boot_block(0, 0x32544F4F42414B), boot_args.ERR_MAGIC, "no magic"),
        (_corrupt_boot_block(1, 99), boot_args.ERR_VERSION, "version"),
        (_corrupt_boot_block(8, MAX_UNITS + 1), boot_args.ERR_UNITS, "units"),
        (_corrupt_boot_block(9 + MAX_UNITS + 2, 1), boot_args.ERR_REGIONS, "regions"),
    ],
)
def test_a_refused_boot_raises_immediately_naming_its_refusal_code(raw, code, reason):
    mem = HostMemory()
    fw = SpecFirmware(mem, BASE, layout=LAYOUT)
    ticks = [0.0]

    def idle():
        ticks[0] += 0.01

    q = NodeQueue(mem, BASE, idle=idle, clock=lambda: ticks[0], layout=LAYOUT)
    q.init()
    # Old firmware behaviour: ka_boot_check exits without writing KA_Q_FW, so
    # the host's wait_ready times out reporting state 'none'.
    with pytest.raises(TimeoutError, match="'state': 'none'"):
        q.wait_ready(timeout=0.02)
    assert ticks[0] >= 0.02
    # New firmware behaviour: ka_boot_check writes KA_Q_ST_FATAL with
    # KA_EXIT_BOOTARGS | reason to KA_Q_FW, and wait_ready raises BootRefused
    # on the first poll without idling.
    ticks[0] = 0.0
    assert fw.boot(raw) == code
    with pytest.raises(BootRefused, match=reason) as e:
        q.wait_ready(timeout=1.0)
    assert e.value.code == code and e.value.reason == reason
    assert ticks[0] == 0.0


#: The firmware's wait bound, in sysnode cycles at SYS_HZ (20 ms).
BOUND = 2_000_000
SYS_HZ = 100e6
IDLE = 600
TIMEOUT = next(k for k, v in L.STATUS.items() if v == "TIMEOUT")


class Stuck:
    """A package whose unit never answers: the firmware's TIMEOUT."""

    def run(self, pkg, binds):
        return type("Done", (), {"status": TIMEOUT, "sent": 0})()


class SlowModel:
    """A model whose firmware ends a stuck wait after `BOUND` of its own
    cycles; an idle advances it `IDLE` cycles and costs the host `seconds`
    of wall clock (the model's speed)."""

    def __init__(self, fw: SpecFirmware, seconds: float) -> None:
        self.fw, self.seconds = fw, seconds
        self.cycles, self.wall = 0, 0.0
        self.since: int | None = None

    def idle(self) -> None:
        self.cycles += IDLE
        self.wall += self.seconds
        if self.fw.rd(LAYOUT.sq_head) == self.fw.rd(LAYOUT.sq_tail):
            return
        self.since = self.cycles if self.since is None else self.since
        if self.cycles - self.since >= BOUND:
            self.fw.step()
            self.since = None

    def modelled(self) -> float:
        return self.cycles / SYS_HZ


def _stuck(seconds: float, clock: str):
    mem = HostMemory()
    fw = SpecFirmware(mem, BASE, Stuck(), layout=LAYOUT)
    model = SlowModel(fw, seconds)
    tick = model.modelled if clock == "modelled" else lambda: model.wall
    q = NodeQueue(
        mem,
        BASE,
        idle=model.idle,
        clock=tick,
        timeout=2 * BOUND / SYS_HZ,
        layout=LAYOUT,
    )
    q.init()
    fw.boot()
    q.wait_ready()
    return q


@pytest.mark.parametrize("seconds", [1e-6, 1e-3, 0.011, 1.0])
def test_a_models_firmware_timeout_lands_before_the_hosts_modelled_wait(seconds):
    # The host's bound (twice the firmware's) counts the model's own time: at
    # any model speed the firmware's TIMEOUT completes the package first.
    q = _stuck(seconds, "modelled")
    with pytest.raises(NodeError, match="TIMEOUT"):
        q.wait(q.submit(package([1])))


def test_a_wall_clock_wait_on_a_slow_model_times_out_before_its_firmware():
    # The mutant: the same wait on the host's wall clock. A model slower than
    # the card ends the host's wait before its firmware's bound has passed.
    q = _stuck(0.011, "wall")
    with pytest.raises(TimeoutError):
        q.wait(q.submit(package([1])))


def test_a_timeout_names_both_rings_pointers():
    # Inject a lost publication: local tail must not masquerade as device readback.
    mem = HostMemory()
    SpecFirmware(mem, BASE, layout=LAYOUT)
    ticks = [0.0]

    def idle():
        ticks[0] += 0.01

    q = NodeQueue(mem, BASE, idle=idle, clock=lambda: ticks[0], layout=LAYOUT)
    q.init()
    tag = q.submit(None, op=LAYOUT.op_nop)
    mem.write64(BASE + LAYOUT.sq_tail, 0)
    with pytest.raises(TimeoutError) as e:
        q.wait(tag, timeout=0.02)
    assert (
        "{'sq_head': 0, 'sq_tail': 1, 'cq_tail': 0, 'cq_head': 0, 'cq_head_tag': 0, "
        "'sq_tail_card': 0, 'cq_head_card': 0}" in str(e.value)
    )


def test_local_tail_as_device_readback_mutant(monkeypatch):
    original = NodeQueue.pointers

    def local_tail(self):
        got = original(self)
        got["sq_tail_card"] = self.sq_tail
        return got

    monkeypatch.setattr(NodeQueue, "pointers", local_tail)
    with pytest.raises(AssertionError):
        test_a_timeout_names_both_rings_pointers()


def test_a_completion_for_no_package_in_flight_raises_naming_its_tag():
    # The CQ tail published before its entry: the host reads the slot's
    # previous contents (tag 7, never submitted) and refuses it.
    mem = HostMemory()
    SpecFirmware(mem, BASE, layout=LAYOUT)
    q = NodeQueue(mem, BASE, idle=lambda: None, clock=lambda: 0.0, layout=LAYOUT)
    q.init()
    q.submit(None, op=LAYOUT.op_nop)
    mem.write_block(BASE + q.geo["cq"], struct.pack("<4Q", 7, 0, 0, 0))
    mem.write_block(BASE + LAYOUT.cq_tail, LAYOUT.line_bytes(1))
    with pytest.raises(RecordSkew, match="stale completion tag 0x7"):
        q.poll()
    assert q.cq_head == 0


def test_a_wait_past_its_wall_clock_alarm_is_over_budget_not_a_timeout():
    mem = HostMemory()
    fw = SpecFirmware(mem, BASE, layout=LAYOUT)
    q = NodeQueue(
        mem, BASE, idle=lambda: None, clock=lambda: 0.0, alarm=1e-3, layout=LAYOUT
    )
    q.init()
    fw.boot()
    with pytest.raises(OverBudget) as e:
        q.wait(q.submit(None, op=LAYOUT.op_nop))
    assert not isinstance(e.value, TimeoutError)


@pytest.mark.parametrize("op", (3, 4, 5))
def test_memory_management_opcodes_are_reserved(op):
    _mem, _fw, q = _queue()
    tag = q.submit(op=op, words=[0, 0x40_0000, 1 << 16, 64])
    with pytest.raises(NodeError, match="BAD_OP"):
        q.wait(tag)
    for name in ("heap", "alloc", "free", "heap_stats"):
        assert not hasattr(q, name)


class Places:
    """A spill target: distinct addresses, at most `cap` bytes held."""

    def __init__(self, cap: int = 1 << 30) -> None:
        self.cap, self.top = cap, 0x4000_0000
        self.live: dict[int, int] = {}

    def alloc(self, n: int) -> int:
        if sum(self.live.values()) + n > self.cap:
            raise MemoryError(f"{n} B do not fit")
        at, self.top = self.top, self.top + n
        self.live[at] = n
        return at

    def free(self, at: int) -> None:
        del self.live[at]


SMALL_REGION = 1 << 16


def _big(q, salt: int = 0) -> bytes:
    """A package one heap and a bit long, its words named by `salt`."""
    n = (q.size - q.heap_off) // 32 + 8
    return package([salt << 32 | k for k in range(n)])


def test_a_package_past_the_heap_runs_from_its_spilled_place():
    places = Places()
    mem, _fw, q = _queue(size=SMALL_REGION, spill=places.alloc, unspill=places.free)
    big = _big(q)
    tag = q.submit(big)
    ((at, n),) = places.live.items()
    assert n >= len(big) and mem.read_block(at, len(big)) == big
    assert q.wait(tag).ok
    assert f"run {len(big)}B" in q.read_stdout()
    assert not places.live
    assert q.run(big).ok and not places.live
    assert q.counters["uploads"] == 2 and q.counters["cache_hits"] == 0


def test_spilled_packages_outlive_heap_wraps_and_go_back_when_dropped():
    places = Places()
    _mem, _fw, q = _queue(size=SMALL_REGION, spill=places.alloc, unspill=places.free)
    big = _big(q)
    q.run(big)
    assert not places.live
    third = (q.size - q.heap_off) // 3 // 32
    for k in range(5):  # small packages enough to wrap the heap twice
        q.run(package([k << 32 | j for j in range(third)]))
    hits = q.counters["cache_hits"]
    q.run(big)
    assert q.counters["cache_hits"] == hits and not places.live
    q.drop_spilled()
    assert not places.live
    q.run(big)
    assert not places.live


def test_a_spill_that_does_not_fit_gives_the_spilled_ones_back_first():
    places = Places()
    _mem, _fw, q = _queue(size=SMALL_REGION, spill=places.alloc, unspill=places.free)
    a, b = _big(q, 1), _big(q, 2)
    places.cap = len(a) + 64
    assert q.run(a).ok
    assert not places.live
    assert q.run(b).ok
    assert not places.live


def test_init_gives_spilled_packages_back():
    places = Places()
    _mem, _fw, q = _queue(size=SMALL_REGION, spill=places.alloc, unspill=places.free)
    q.run(_big(q))
    q.init()
    assert not places.live


def test_stdout_wraps_and_stdin_lands_whole_lines():
    mem, fw, q = _queue(cq_n=3, so_bytes=64, si_bytes=64)
    for k in range(3):
        fw.print(f"line {k} of a ring, longer than a third of it\n")
        assert q.read_stdout() == f"line {k} of a ring, longer than a third of it\n"
    q.stdin("hello")
    # 3 CQ entries end mid-line; the stdout ring starts on the next line.
    so = BASE + LAYOUT.sq + 64 * q.sq_n + 32 * q.cq_n + 32
    assert so % LAYOUT.line == 0
    assert mem.read_block(so + 64, 8)[:5] == b"hello"
    assert mem.read64(BASE + LAYOUT.si_wr) == 5
    assert mem.host_writes and all(
        a % LAYOUT.line == 0 and n % LAYOUT.line == 0 for a, n in mem.host_writes
    )
