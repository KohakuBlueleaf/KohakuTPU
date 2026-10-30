"""NodeQueue: submit packages to a node's dispatcher and wait for completions.

    q = NodeQueue(mem, base=dram_base + 0x10_0000, idle=lambda: sim.run(1000))
    q.init()                                  # before the firmware boots
    ... boot the firmware with BootArgs(queue=q.base, ...) ...
    q.wait_ready()
    done = q.run(package_bytes, bindings=[a, b, c])

`mem` addresses UNIT-GLOBAL memory, so `base` and every binding are the
addresses the firmware uses. Polling is the design: `idle` is what a poll does
between reads (advance the model, or sleep on a card). Packages live in a heap
in the region, found again by digest (docs/spec/node-queue.md s6).
"""

import os
import re
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from kohakuaccel.driver.device import mover as MV
from kohakuaccel.driver.node.boot.args import EXIT_BOOTARGS, EXIT_MASK, REFUSALS
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.execution import prepare as prepare_list

#: The default region: rings, then a package heap.
REGION_BYTES = 1 << 20
#: Bindings per submission slot: the firmware's KA_MAX_BUFFERS.
MAX_BINDINGS = 64
#: A package's own granule (package-format.md): whole 32-byte lines.
PACKAGE_LINE = 32


def engine_stat(stat: int) -> dict:
    """The dispatch engine's `STAT` (docs/spec/dispatch-engine.md), the value
    of a TIMEOUT waiting on ENGINE (RUL.032). `issued` is the head entry's
    index in the package's queued entries."""
    return {
        "magic": stat >> 48,
        "depth": 1 << (stat >> 40 & 0xFF),
        "counter_count": 1 << (stat >> 36 & 0xF),
        "overflow": bool(stat >> 35 & 1),
        "clearing": bool(stat >> 34 & 1),
        "head_blocked": bool(stat >> 33 & 1),
        "enabled": bool(stat >> 32 & 1),
        "issued": stat >> 16 & 0xFFFF,
        "queued": stat & 0xFFFF,
    }


def failure_snapshot(console: str) -> dict | None:
    """Decode DBG.FAIL.006's pre-clear PKGF snapshot; old images return None.

    Reads are sequential, not an atomic hardware latch. Counters and MOVES
    are modulo the engine's counter width; MVSTAT.done is free-running.
    """
    lines = console.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("PKGF de stat ")]
    if not starts:
        return None
    at = starts[-1]
    match = re.fullmatch(
        r"PKGF de stat ([0-9a-fA-F]+) moves ([0-9a-fA-F]+) mvstat ([0-9a-fA-F]+)"
        r" xfdata ([0-9a-fA-F]+)",
        lines[at],
    )
    if match is None:
        raise ValueError("DBG.FAIL.006: malformed snapshot header")
    stat, moves, mvstat, xfdata = (int(w, 16) for w in match.groups())
    if any(w >= 1 << 64 for w in (stat, moves, mvstat, xfdata)):
        raise ValueError("DBG.FAIL.006: snapshot word exceeds 64 bits")
    decoded = engine_stat(stat)
    nctr = min(16, decoded["counter_count"]) if decoded["magic"] == 0xDE01 else 0
    if at + 1 >= len(lines) or not re.fullmatch(
        r"PKGF de ctr(?: [0-9a-fA-F]+)*", lines[at + 1]
    ):
        raise ValueError("DBG.FAIL.006: missing snapshot counters")
    words = lines[at + 1].split()[3:]
    if len(words) != nctr or any(not re.fullmatch(r"[0-9a-fA-F]+", w) for w in words):
        raise ValueError("DBG.FAIL.006: malformed snapshot counters")
    counters = [int(w, 16) for w in words]
    if any(w >= 1 << 64 for w in counters):
        raise ValueError("DBG.FAIL.006: snapshot counter exceeds 64 bits")
    fault = mvstat >> 28 & 0xF
    return {
        "raw": {"stat": stat, "moves": moves, "mvstat": mvstat, "xfdata": xfdata},
        "stat": decoded,
        "moves": {"issued": moves >> 32, "done": moves & 0xFFFFFFFF},
        "mover": {
            "room": bool(mvstat >> 33 & 1),
            "busy": bool(mvstat >> 32 & 1),
            "fault": fault,
            "fault_name": MV.FAULTS.get(fault, "unknown"),
            "done": mvstat & 0x0FFFFFFF,
        },
        "counters": counters,
    }


_STATUS_CODES = {name.lower(): code for code, name in L.STATUS.items()}


@dataclass
class Completion:
    """One CQ entry, decoded."""

    tag: int
    status: int
    detail: int
    step: int
    cycles: int
    value: int
    #: Run record decoded with the machine schema (L0.NQ.056).
    record: dict | None = None
    ok_status: int = field(default=_STATUS_CODES["ok"], repr=False, compare=False)
    timeout_status: int = field(
        default=_STATUS_CODES["timeout"], repr=False, compare=False
    )
    unit_fault_status: int = field(
        default=_STATUS_CODES["unit_fault"], repr=False, compare=False
    )
    mover_fault_status: int = field(
        default=_STATUS_CODES["mover_fault"], repr=False, compare=False
    )

    @classmethod
    def from_bytes(
        cls,
        raw: bytes,
        layout: L.Layout,
        record: bytes | None = None,
        schema: L.Record | None = None,
    ) -> "Completion":
        words = struct.unpack(f"<{len(layout.cq_offsets)}Q", raw)
        tag = words[layout.cq_tag // 8]
        word = words[layout.cq_status // 8]
        cycles = words[layout.cq_cycles // 8]
        value = words[layout.cq_value // 8]
        mask = layout.cq_status_mask
        decoded = (
            None
            if record is None
            else (layout.record if schema is None else schema).decode(record)
        )
        return cls(
            tag,
            word & mask,
            (word >> 16) & mask,
            word >> 32,
            cycles,
            value,
            decoded,
            ok_status=layout.ok,
            timeout_status=layout.timeout,
            unit_fault_status=layout.unit_fault,
            mover_fault_status=layout.mover_fault,
        )

    @property
    def ok(self) -> bool:
        return self.status == self.ok_status

    @property
    def name(self) -> str:
        return L.STATUS.get(self.status, f"{self.status:#x}")

    def describe(self) -> str:
        what = self.name
        if self.status == self.timeout_status:
            what += f" waiting on {L.WAITS.get(self.detail, self.detail)}"
            if L.WAITS.get(self.detail) == "engine":
                s = engine_stat(self.value)
                what += (
                    f" (head entry {s['issued']} {'blocked' if s['head_blocked'] else 'free'},"
                    f" {s['queued']} queued)"
                )
        elif self.status == self.unit_fault_status:
            what += f" from unit {self.detail}, signal word {self.value:#x}"
        elif self.status == self.mover_fault_status:
            what += f" {self.detail}: {MV.FAULTS.get(self.detail, 'unknown')}"
        return f"tag {self.tag:#x}: {what} at step {self.step} ({self.cycles} cycles)"


class NodeError(RuntimeError):
    """A package the node refused or failed, with its completion."""

    def __init__(self, completion: Completion, console: str = "") -> None:
        self.completion = completion
        self.console = console
        self.snapshot_error: str | None = None
        try:
            self.snapshot = failure_snapshot(console)
        except ValueError as error:
            self.snapshot = None
            self.snapshot_error = str(error)
        #: Filled by the card runtime on a debug or dumping run: the decoded
        #: debug records, the RUL.075 rerun's, and the buffer dumps.
        self.debug: dict | None = None
        self.rerun: dict | None = None
        self.dump: dict | None = None
        tail = f"\nnode said:\n{console.rstrip()}" if console.strip() else ""
        super().__init__(completion.describe() + tail)

    def __reduce__(self):
        # Rebuilt from its own arguments: a process pool pickles it back.
        return (NodeError, (self.completion, self.console))


class FirmwareFatal(RuntimeError):
    """The firmware stopped fatally while the host waited: its line's state
    and exit code, and the stdout ring's text."""

    def __init__(self, state: dict, console: str = "") -> None:
        self.state = state
        self.console = console
        tail = f"\nnode said:\n{console.rstrip()}" if console.strip() else ""
        super().__init__(f"firmware stopped fatally, code {state['fatal']:#x}{tail}")

    def __reduce__(self):
        return (FirmwareFatal, (self.state, self.console))


class BootRefused(FirmwareFatal):
    """The firmware refused its boot block or queue region before ready: its
    refusal code (`code`, `reason`), the firmware line's `state`, and any
    console text."""

    def __init__(self, state: dict, console: str = "") -> None:
        self.state = state
        self.console = console
        fatal = state["fatal"]
        self.code = (
            fatal & ~EXIT_MASK if (fatal & EXIT_MASK) == EXIT_BOOTARGS else fatal
        )
        self.reason = REFUSALS.get(self.code, f"{self.code:#x}")
        tail = f"\nnode said:\n{console.rstrip()}" if console.strip() else ""
        RuntimeError.__init__(
            self,
            f"firmware refused boot ({self.reason}, code {self.code:#x}): {state}{tail}",
        )

    def __reduce__(self):
        return (BootRefused, (self.state, self.console))


class OverBudget(RuntimeError):
    """A wait ran past the queue's `alarm` in wall seconds before its own
    bound (the queue's clock) ended: the host ran out of patience, the node
    was not judged. Never a `TimeoutError`."""

    def __init__(self, what: str, state: dict, seconds: float) -> None:
        self.what, self.state, self.seconds = what, state, seconds
        super().__init__(f"{what}: over the {seconds}s wall-clock alarm: {state}")

    def __reduce__(self):
        return (OverBudget, (self.what, self.state, self.seconds))


class RecordSkew(RuntimeError):
    """The run record's tag differs from its CQ entry's (L0.NQ.056)."""


@dataclass
class PreparedPackage:
    """One graph-owned upload, valid until close or the queue is initialized."""

    queue: object
    key: bytes
    address: int
    nbytes: int
    generation: int
    closed: bool = False

    def close(self):
        if not self.closed:
            self.closed = True
            q = self.queue
            if self.generation == q._generation:
                q._package_pins[self.key] -= 1
                if not q._package_pins[self.key]:
                    del q._package_pins[self.key]
                q._release_package(self.key)


class NodeQueue:
    """The host side of one node's queue region."""

    def __init__(
        self,
        mem,
        base: int,
        size: int = REGION_BYTES,
        sq_n: int = 8,
        cq_n: int = 16,
        so_bytes: int = 4096,
        si_bytes: int = 256,
        idle=None,
        poll_seconds: float = 0.0,
        spill=None,
        spin: float = 0.0,
        unspill=None,
        timeout: float = 300.0,
        clock=time.monotonic,
        alarm: float = 0.0,
        record: L.Record | None = None,
        head_cache: bool | None = None,
        *,
        layout: L.Layout,
    ) -> None:
        if base % 4096:
            raise ValueError(f"a queue region is page aligned; {base:#x} is not")
        mode = os.environ.get("KOHAKUTPU_SQ_HEAD_CACHE", "0")
        if head_cache is None and mode not in ("0", "1"):
            raise ValueError("KOHAKUTPU_SQ_HEAD_CACHE must be 0 or 1")
        self.head_cache = mode == "1" if head_cache is None else head_cache
        self.batch_size = int(os.environ.get("KOHAKUTPU_SUBMIT_BATCH", "1"))
        if not 1 <= self.batch_size <= min(sq_n, cq_n):
            raise ValueError("submit batch must fit both SQ and CQ rings")
        self.cq_bulk = os.environ.get("KOHAKUTPU_CQ_BULK", "0")
        if self.cq_bulk not in ("0", "1"):
            raise ValueError("KOHAKUTPU_CQ_BULK must be 0 or 1")
        self.cq_bulk = self.cq_bulk == "1"
        self.resident_bytes = int(os.environ.get("KOHAKUTPU_PACKAGE_CACHE_BYTES", "0"))
        self.backoff = float(os.environ.get("KOHAKUTPU_POLL_BACKOFF", "0"))
        if self.resident_bytes < 0 or not 0 <= self.backoff <= 0.1:
            raise ValueError("nonnegative cache bytes and backoff in [0, 0.1] required")
        self._native_lock = threading.RLock()
        self.layout = layout
        self._idle_packages = OrderedDict()
        self._idle_bytes = 0
        self._staged = []
        self._broken = False
        self._poll_state = None
        self._sq_head_seen = 0
        self.native_poll = None
        self.native_exchange = None
        self.native_run = None
        self.binding_limit = MAX_BINDINGS
        self.mem = mem
        self.base = base
        self.size = size
        self.sq_n, self.cq_n = sq_n, cq_n
        self.so_bytes, self.si_bytes = so_bytes, si_bytes
        self.speak(layout.version, record, layout=layout)
        self.idle = idle
        self.poll_seconds = poll_seconds
        #: Seconds a `wait` polls back to back before it idles between polls:
        #: where a poll is cheap (PCIe, ~30 us) and a package short, an idle
        #: of any length is the wait.
        self.spin = spin
        #: Seconds any wait polls before it raises `TimeoutError`, unless the
        #: call names its own bound: no wait on the node is unbounded.
        self.timeout = timeout
        #: `clock() -> seconds` the wait bounds count: the node's time (wall on
        #: a card, modelled on a model, as the firmware's own bounds count).
        self.clock = clock
        #: Wall seconds any one wait runs before it raises `OverBudget`
        #: (0: none), whatever its bound on `clock`.
        self.alarm = alarm
        #: `spill(nbytes) -> address` places a package larger than the heap
        #: anywhere the node reads (an entry carries the package's address);
        #: `unspill(address)` gives the place back when the package is dropped.
        self.spill = spill
        self.unspill = unspill
        #: digest -> address of each package placed outside the heap
        self._spilled: dict[bytes, int] = {}
        self._package_tags: dict[int, bytes] = {}
        self._list_tags: dict = {}
        self._package_pins: dict[bytes, int] = {}
        self._prepared_keys: set[bytes] = set()
        self._generation = -1
        self._discarded_generation = -1
        self._heap_free: list[tuple[int, int]] = []
        self.sq_tail = self.cq_head = self.so_rd = self.si_wr = 0
        self._cache: OrderedDict[bytes, tuple[int, int]] = OrderedDict()
        self._heap_top = self.heap_off
        self._done: dict[int, Completion] = {}
        #: Final stdout counts, retained for callers that take them.
        self.ends: dict[int, int] = {}
        self.track_ends = False
        self._outstanding: set[int] = set()
        self._stdin = bytearray(si_bytes)
        self.progress: list[Completion] = []
        self._tag = 0
        self.console = ""
        self.console_at = 0
        self.counters = dict.fromkeys(
            ("submitted", "uploads", "upload_bytes", "cache_hits", "polls"), 0
        )

    # ----------------------------------------------------------------- setup
    def speak(
        self,
        version: int,
        record: L.Record | None = None,
        layout: L.Layout | None = None,
    ) -> None:
        """Select the image's queue version and schema record layout before init."""
        with self._native_lock:
            if layout is not None:
                self.layout = layout
            if version not in self.layout.versions:
                raise ValueError(
                    f"queue version {version}; the host speaks {self.layout.versions}"
                )
            rings = version >= self.layout.version
            if record is not None and not rings:
                raise ValueError(f"a version {version} region has no run records")
            self.version, self.record = version, record
            line = self.layout.line
            self.geo = self.layout.geometry(
                self.sq_n,
                self.cq_n,
                self.so_bytes,
                self.si_bytes,
                self.layout.rec_bytes if rings else 0,
            )
            self.bind_off = -(-self.geo["end"] // line) * line
            self.heap_off = (
                -(-(self.bind_off + self.sq_n * MAX_BINDINGS * 8) // 4096) * 4096
            )
            if self.heap_off >= self.size:
                raise ValueError(f"{self.size:#x} bytes leaves no package heap")
            self._heap_top = self.heap_off

    def _info_line(self) -> bytes:
        return self.layout.info_line(
            self.version,
            self.sq_n,
            self.cq_n,
            self.so_bytes,
            self.si_bytes,
            self._generation,
        )

    def init(self) -> None:
        """Write the region's header and zero every index. Before the firmware boots."""
        with self._native_lock:
            self._generation += 1
            info = self._info_line()
            self.mem.write_block(
                self.base + self.layout.info,
                info + bytes(self.layout.sq - self.layout.line),
            )
            self.sq_tail = self.cq_head = self.so_rd = self.si_wr = 0
            self._sq_head_seen = 0
            self._staged.clear()
            self._broken = False
            self._poll_state = None
            self.console, self.console_at = "", 0
            # Nothing submitted before this is waited for: a node restarted
            # mid-run never completes it.
            self._list_tags.clear()
            self._package_tags.clear()
            self._package_pins.clear()
            self._prepared_keys.clear()
            self._unspill_all()
            self._cache.clear()
            self._heap_free.clear()
            self._heap_top = self.heap_off
            self._done.clear()
            self.ends.clear()
            self._outstanding.clear()
            self._stdin = bytearray(self.si_bytes)

    def state(self) -> dict:
        """The firmware's line: state, ABI, packages done, fatal code."""
        raw = self.mem.read_block(self.base + self.layout.fw, self.layout.fw_bytes)
        return self.layout.decode_fw(raw)

    def pointers(self) -> dict:
        """Both rings as the card and the host see them: the firmware's SQ
        head and CQ tail, the host's SQ tail and CQ head, and the tag of the
        CQ entry at the host's head. *_card reads back host-owned publications."""
        cq_tail = self.mem.read64(self.base + self.layout.cq_tail)
        at = self.cq_head % self.cq_n
        head_tag = self.mem.read64(
            self.base + self.geo["cq"] + at * self.layout.cq_bytes
        )
        return {
            "sq_head": self._sq_head(),
            "sq_tail": self.sq_tail,
            "cq_tail": cq_tail,
            "cq_head": self.cq_head,
            "cq_head_tag": head_tag,
            "sq_tail_card": self.mem.read64(self.base + self.layout.sq_tail),
            "cq_head_card": self.mem.read64(self.base + self.layout.cq_head),
        }

    def units(self) -> list[tuple[str, int, int, int]]:
        """The units the firmware serves -- given at boot or found itself --
        as ``(type name, mesh, x, y)``. Published before it reports ready."""
        line = self.layout.line
        n = self.mem.read64(self.base + self.layout.units)
        raw = (
            self.mem.read_block(
                self.base + self.layout.units + line, -(-n * 8 // line) * line
            )
            if n
            else b""
        )
        out = []
        for (w,) in struct.iter_unpack("<Q", raw[: n * 8]):
            kind = bytes(((w >> 40) & 0xFF, (w >> 32) & 0xFF)).decode(
                "ascii", "replace"
            )
            out.append((kind, (w >> 16) & 0xFF, w & 0xFF, (w >> 8) & 0xFF))
        return out

    def _check_ready_state(self, st: dict) -> dict | None:
        if st["state"] == "ready":
            self._stdout()
            return st
        if st["state"] == "fatal":
            text = self.read_stdout()
            if (st["fatal"] & EXIT_MASK) == EXIT_BOOTARGS:
                raise BootRefused(st, text)
            raise FirmwareFatal(st, text)
        # `init` zeroes the line, so a STOPPED here is this boot's and final.
        if st["state"] == "stopped":
            raise RuntimeError(
                f"firmware stopped before ready: {st}\n{self.read_stdout()}"
            )
        return None

    def wait_ready(self, timeout: float = 120.0) -> dict:
        """Poll until the firmware reports READY. Raises :class:`BootRefused`
        on a refused boot, :class:`FirmwareFatal` on a fatal trap,
        `RuntimeError` on a STOPPED line, and :class:`TimeoutError`."""
        start = self._start()
        while True:
            state = self.state()
            got = self._check_ready_state(state)
            if got is not None:
                return got
            if self._waited(start, "ready") >= timeout:
                raise TimeoutError(f"firmware never reported ready: {state}")
            self._idle()

    # ----------------------------------------------------------------- heap
    def upload(self, package: bytes) -> int:
        """Where `package` is in the heap, uploading it the first time.

        With `spill`, every package uses host-owned shared memory. Final
        completion retires its last submitted reference. Without `spill`,
        retired queue-local spans are reused; a full heap resets only when
        nothing submitted is outstanding.
        """
        with self._native_lock:
            if len(package) % PACKAGE_LINE:
                raise ValueError(f"a package is whole {PACKAGE_LINE}-byte lines")
            key = bytes(package)
            got = self._cache.get(key)
            if got is not None:
                self._cache.move_to_end(key)
                self._idle_bytes -= self._idle_packages.pop(key, 0)
                self.counters["cache_hits"] += 1
                return got[0]
            # Written in whole queue lines: the host's write granule may be wider
            # than the package's.
            padded = package + bytes(-len(package) % self.layout.line)
            if self.spill is not None:
                try:
                    at = self.spill(len(padded))
                except MemoryError:
                    self.drop_spilled()
                    at = self.spill(len(padded))
                try:
                    self.mem.write_block(at, padded)
                except BaseException:
                    if self.unspill is not None:
                        self.unspill(at)
                    raise
                self._cache[key] = (at, len(package))
                self._spilled[key] = at
                self.counters["uploads"] += 1
                self.counters["upload_bytes"] += len(package)
                return at
            if self.heap_off + len(padded) > self.size:
                raise ValueError(
                    f"a {len(package)}-byte package exceeds the {self.size - self.heap_off}-byte heap"
                )
            recycled = next(
                (
                    (i, at, size)
                    for i, (at, size) in enumerate(self._heap_free)
                    if size >= len(padded)
                ),
                None,
            )
            if recycled is not None:
                i, at, size = recycled
                self.mem.write_block(at, padded)
                self._heap_free.pop(i)
                if size > len(padded):
                    self._heap_free.append((at + len(padded), size - len(padded)))
                self._cache[key] = (at, len(package))
                self.counters["uploads"] += 1
                self.counters["upload_bytes"] += len(package)
                return at
            if self._heap_top + len(padded) > self.size:
                self._drain_all()
                # The heap's packages are overwritten from here; spilled ones stay.
                for k in [k for k in self._cache if k not in self._spilled]:
                    del self._cache[k]
                self._heap_top = self.heap_off
                self._heap_free.clear()
            at = self.base + self._heap_top
            self.mem.write_block(at, padded)
            self._heap_top += len(padded)
            self._cache[key] = (at, len(package))
            self.counters["uploads"] += 1
            self.counters["upload_bytes"] += len(package)
            return at

    def prepare(self, package: bytes) -> PreparedPackage:
        """Pin a graph's upload outside the recyclable queue heap."""
        with self._native_lock:
            if self.spill is None:
                raise ValueError(
                    "prepared packages require a host-owned spill allocator"
                )
            address = self.upload(package)
            key = bytes(package)
            self._package_pins[key] = self._package_pins.get(key, 0) + 1
            self._prepared_keys.add(key)
            return PreparedPackage(self, key, address, len(package), self._generation)

    @property
    def prepared_bytes(self):
        """Prepared uploads, including closed pins still held by submitted tags."""
        line = self.layout.line
        return sum(
            (self._cache[k][1] + line - 1) // line * line for k in self._prepared_keys
        )

    def has_prepared(self, package: bytes) -> bool:
        return bytes(package) in self._prepared_keys

    def is_cached(self, package: bytes) -> bool:
        return bytes(package) in self._cache

    def prepared_addresses(self) -> set[int]:
        return {self._cache[k][0] for k in self._prepared_keys}

    def discard_packages(self):
        """Invalidate uploads after the backend has stopped accessing memory."""
        with self._native_lock:
            if (
                self._discarded_generation == self._generation
                and not self._list_tags
                and not self._package_tags
                and not self._package_pins
                and not self._prepared_keys
                and not self._spilled
                and not self._cache
            ):
                return
            self._list_tags.clear()
            self._package_tags.clear()
            self._package_pins.clear()
            self._prepared_keys.clear()
            self._generation += 1
            self._discarded_generation = self._generation
            self.mem.write_block(self.base + self.layout.info, self._info_line())
            self._unspill_all()
            self._cache.clear()

    prepare_list = prepare_list

    def submit_list(self, prepared, publishing=None, native=False):
        with self._native_lock:
            if (
                prepared.queue is not self
                or prepared.closed
                or prepared.generation != self._generation
            ):
                raise ValueError(
                    "execution list is closed or belongs to another queue epoch"
                )

            if self._broken:
                raise RuntimeError("queue publication failed; reset before submitting")
            if (
                native
                and self.native_exchange is not None
                and not self._staged
                and not self._package_tags
                and not self._list_tags
                and not (self._outstanding - self._done.keys())
            ):
                return self.native_exchange.submit(prepared, publishing)

            def publish():
                prepared.inflight += 1
                self._list_tags[self._tag] = prepared
                if publishing is not None:
                    publishing()

            return self.submit(
                prepared.descriptor,
                op=self.layout.op_list,
                words=self.layout.list_tail(
                    prepared.descriptor.address,
                    prepared.count,
                    prepared.generation,
                ),
                publishing=publish,
            )

    # --------------------------------------------------------------- submit
    def submit(
        self,
        package: bytes | None = None,
        bindings=None,
        op: int | None = None,
        timeout_cycles: int = 0,
        flags: int = 0,
        arg: int = 0,
        words=None,
        publishing=None,
    ) -> int:
        """Queue one entry; returns its tag. Waits while the SQ is full, at
        most the queue's `timeout` (then :class:`TimeoutError`).

        `words` replaces entry words 2.. whole (at most 6), for the opcodes
        whose arguments are not a package's (including reserved-opcode probes).
        `publishing`, if given, runs immediately before the first queue entry write.
        """
        with self._native_lock:
            op = self.layout.op_run if op is None else op
            if (
                self.native_run is not None
                and package is not None
                and op == self.layout.op_run
                and not timeout_cycles
                and not flags
                and not arg
                and words is None
            ):
                return self.native_run(self, package, list(bindings or ()), publishing)
            if self._broken:
                raise RuntimeError("queue publication failed; reset before submitting")
            start = self._start()
            while not self._sq_room():
                self.poll()
                waited = self._waited(start, "a full SQ")
                if waited > self.timeout:
                    raise TimeoutError(
                        f"the SQ stayed full for {self.timeout}s: {self.state()}"
                    )
                if waited >= self.spin:
                    self._idle()
            slot = self.sq_tail % self.sq_n
            self._tag += 1
            tag = self._tag
            pkg, nbytes, bind, nbind = arg, 0, 0, 0
            try:
                if isinstance(package, PreparedPackage):
                    if (
                        package.queue is not self
                        or package.closed
                        or package.generation != self._generation
                        or not self._package_pins.get(package.key)
                    ):
                        raise ValueError(
                            "prepared package is closed or belongs to another queue epoch"
                        )
                    pkg, nbytes = package.address, package.nbytes
                    self._package_tags[tag] = package.key
                elif package is not None:
                    pkg, nbytes = self.upload(package), len(package)
                    self._package_tags[tag] = bytes(package)
                if bindings:
                    nbind = len(bindings)
                    if nbind > MAX_BINDINGS:
                        raise ValueError(
                            f"{nbind} bindings; the firmware binds {MAX_BINDINGS}"
                        )
                    blob = struct.pack(f"<{nbind}Q", *bindings)
                    blob += bytes(-len(blob) % self.layout.line)
                    bind = self.base + self.bind_off + slot * MAX_BINDINGS * 8
                    self.mem.write_block(bind, blob)
                if words is not None:
                    max_words = self.layout.sq_tail_words
                    if len(words) > max_words:
                        raise ValueError(
                            f"{len(words)} argument words; an entry carries {max_words}"
                        )
                    tail = list(words) + [0] * (max_words - len(words))
                    entry = self.layout.entry_bytes(
                        op | nbind << self.layout.nbind_shift, tag, *tail
                    )
                else:
                    entry = self.layout.entry_bytes(
                        *self.layout.run_entry(
                            tag,
                            pkg,
                            nbytes,
                            bind,
                            nbind,
                            timeout_cycles,
                            flags,
                            op=op,
                        )
                    )
            except BaseException:
                self._retire_package(tag)
                raise
            # Retain through publication, including a failed transport write.
            self._outstanding.add(tag)
            if publishing is not None:
                publishing()
            self._staged.append((slot, entry))
            self.sq_tail += 1
            if len(self._staged) >= self.batch_size:
                self.flush()
            self.counters["submitted"] += 1
            return tag

    def flush(self):
        """Write contiguous SQ runs, then publish one tail, never the reverse."""
        with self._native_lock:
            if self._broken:
                raise RuntimeError("queue publication failed; reset before flushing")
            if not self._staged:
                return
            try:
                start = previous = self._staged[0][0]
                blob = bytearray()
                for slot, entry in self._staged:
                    if slot != previous and slot != previous + 1:
                        self.mem.write_block(
                            self.base + self.geo["sq"] + start * self.layout.sq_bytes,
                            bytes(blob),
                        )
                        start, blob = slot, bytearray()
                    blob.extend(entry)
                    previous = slot
                self.mem.write_block(
                    self.base + self.geo["sq"] + start * self.layout.sq_bytes,
                    bytes(blob),
                )
                self.mem.write_block(
                    self.base + self.layout.sq_tail,
                    self.layout.line_bytes(self.sq_tail),
                )
            except BaseException:
                self._broken = True
                raise
            self._staged.clear()

    def _sq_room(self) -> bool:
        """A stale observed head underestimates space; refresh only at pressure."""
        if not self.head_cache or self.sq_tail - self._sq_head_seen >= self.sq_n:
            self._sq_head_seen = self._sq_head()
        return self.sq_tail - self._sq_head_seen < self.sq_n

    def _sq_head(self) -> int:
        return self.mem.read64(self.base + self.layout.sq_head)

    # ----------------------------------------------------------------- poll
    def poll(self, wait_seconds: float = 0.0) -> list[Completion]:
        """Read every completion that has arrived; returns them in order."""
        with self._native_lock:
            self.flush()
            self.counters["polls"] += 1
            snapshot = None
            layout = self.layout
            tail_bytes = layout.tail_poll_bytes
            if self.native_poll is not None:
                header, polls = self.native_poll(wait_seconds)
                self.counters["polls"] += polls - 1
                raw = header[
                    layout.cq_tail - layout.fw : layout.cq_tail - layout.fw + tail_bytes
                ]
                self._poll_state = layout.decode_fw(header)
                head = struct.unpack_from("<Q", header, layout.sq_head - layout.fw)[0]
                self._sq_head_seen = max(self._sq_head_seen, head)
            elif self.cq_bulk:
                # Tail precedes records in address order; slots cannot be reused before ACK.
                snapshot = self.mem.read_block(
                    self.base + layout.fw, self.geo["end"] - layout.fw
                )
                raw = snapshot[
                    layout.cq_tail - layout.fw : layout.cq_tail - layout.fw + tail_bytes
                ]
                self._poll_state = layout.decode_fw(snapshot)
                head = struct.unpack_from("<Q", snapshot, layout.sq_head - layout.fw)[0]
                self._sq_head_seen = max(self._sq_head_seen, head)
            else:
                raw = self.mem.read_block(self.base + layout.cq_tail, tail_bytes)
            tail = struct.unpack_from("<Q", raw, 0)[0]
            so_wr = struct.unpack_from("<Q", raw, layout.so_wr_from_cq_tail)[0]
            if so_wr != self.so_rd:
                self._stdout(so_wr)
            got = []
            if not self.cq_head <= tail <= self.cq_head + self.cq_n:
                raise RecordSkew("completion tail exceeds the ring")
            if tail != self.cq_head:
                n = tail - self.cq_head
                finalized = set(self._done)
                for k in range(n):
                    at = (self.cq_head + k) % self.cq_n
                    off = self.geo["cq"] + at * layout.cq_bytes
                    e = (
                        self.mem.read_block(self.base + off, layout.cq_bytes)
                        if snapshot is None
                        else snapshot[
                            off - layout.fw : off - layout.fw + layout.cq_bytes
                        ]
                    )
                    rec_raw = None
                    if self.record is not None:
                        off = self.geo["rec"] + at * layout.rec_bytes
                        rec_raw = (
                            self.mem.read_block(self.base + off, layout.rec_bytes)
                            if snapshot is None
                            else snapshot[
                                off - layout.fw : off - layout.fw + layout.rec_bytes
                            ]
                        )
                    c = Completion.from_bytes(
                        e, layout, rec_raw, self.record or layout.record
                    )
                    if c.record is not None and c.record["tag"] != c.tag:
                        raise RecordSkew(
                            f"CQ entry {self.cq_head + k} is tag {c.tag:#x}, its record "
                            f"tag {c.record['tag']:#x}"
                        )
                    if c.tag not in self._outstanding or c.tag in finalized:
                        raise RecordSkew(
                            f"unexpected or stale completion tag {c.tag:#x}"
                        )
                    if c.status != layout.progress:
                        finalized.add(c.tag)
                    got.append(c)
                # Validate the whole snapshot before retiring any allocation; HOSTL ownership ends after ACK.
                try:
                    self.mem.write_block(
                        self.base + layout.cq_head, layout.line_bytes(tail)
                    )
                except BaseException:
                    self._broken = True
                    raise
                self.cq_head = tail
                for c in got:
                    if c.status == layout.progress:
                        self.progress.append(c)
                    else:
                        self._done[c.tag] = c
                        self._retire_package(c.tag)
                        if c.record is not None and self.track_ends:
                            self.ends[c.tag] = c.record["so_wr"]
                for c in got:
                    if c.status != layout.progress:
                        prepared = self._list_tags.pop(c.tag, None)
                        if prepared is not None:
                            prepared.acknowledge()
            return got

    def wait(
        self, tag: int, timeout: float | None = None, check: bool = True, service=None
    ) -> Completion:
        """Poll until `tag` completes, at most `timeout` seconds (the queue's
        `timeout` when None). Raises :class:`NodeError` on a failed
        completion (unless `check` is False), :class:`FirmwareFatal` once the
        firmware's line reads FATAL (checked before each idle, DBG.SESS.004),
        and :class:`TimeoutError` (:class:`OverBudget` past `alarm`)."""
        with self._native_lock:
            timeout = self.timeout if timeout is None else timeout
            start = self._start()
            delay = min(self.backoff, 0.00001)
            while tag not in self._done:
                waited = self._waited(start, f"tag {tag:#x}")
                if waited > timeout:
                    # A completion published during the last idle outranks the deadline.
                    self.poll()
                    if tag in self._done:
                        break
                    raise TimeoutError(
                        f"tag {tag:#x} did not complete in {timeout}s: {self.state()} "
                        f"{self.pointers()}"
                    )
                polled = (
                    self.poll(min(0.0001, max(0.0, timeout - waited)))
                    if self.native_poll is not None
                    else self.poll()
                )
                if not polled and tag not in self._done:
                    if service is not None:
                        service()
                    if delay:
                        time.sleep(delay)
                        delay = min(self.backoff, delay * 2)
                    if waited >= self.spin:
                        st = (
                            self._poll_state
                            if self.cq_bulk or self.native_poll is not None
                            else self.state()
                        )
                        if st["state"] == "fatal":
                            if (st["fatal"] & EXIT_MASK) == EXIT_BOOTARGS:
                                raise BootRefused(st, self.read_stdout())
                            raise FirmwareFatal(st, self.read_stdout())
                        self._idle()
            c = self._done.pop(tag)
            self._outstanding.discard(tag)
            if check and not c.ok:
                text = (
                    self.read_stdout_to(c.record["so_wr"])
                    if c.record
                    else self.read_stdout()
                )
                raise NodeError(c, text)
            return c

    def take(self, tag: int) -> Completion | None:
        """`tag`'s final completion if it has arrived (one poll), else None:
        :meth:`wait` for a caller that owns the polling loop."""
        with self._native_lock:
            if tag not in self._done:
                self.poll()
            c = self._done.pop(tag, None)
            if c is not None:
                self._outstanding.discard(tag)
            return c

    def pending(self) -> set[int]:
        """The tags submitted whose final completion has not arrived (after
        one poll), a package a wait gave up on among them."""
        self.poll()
        return self._outstanding - set(self._done)

    def run(self, package: bytes, bindings=None, **kw) -> Completion:
        """Submit and wait."""
        timeout = kw.pop("timeout", None)
        return self.wait(self.submit(package, bindings, **kw), timeout=timeout)

    def nop(self) -> Completion:
        return self.wait(self.submit(None, op=self.layout.op_nop))

    def stop(self, value: int = 0, timeout: float | None = None) -> Completion:
        """Ask the firmware to leave its loop; it exits with STOP | value.
        Raises :class:`TimeoutError` past `timeout` (see :meth:`wait`)."""
        return self.wait(
            self.submit(None, op=self.layout.op_stop, arg=value), timeout=timeout
        )

    def drop_spilled(self) -> None:
        """Every package placed outside the heap given back (`unspill`) once
        nothing submitted is outstanding; a later submit places it again."""
        with self._native_lock:
            if self._spilled:
                self._drain_all()
                self._unspill_all()

    def _retire_package(self, tag: int) -> None:
        key = self._package_tags.pop(tag, None)
        if key is not None:
            self._release_package(key)

    def _release_package(self, key):
        with self._native_lock:
            if key in self._package_tags.values() or self._package_pins.get(key):
                return
            if (
                self.resident_bytes
                and key not in self._prepared_keys
                and key in self._spilled
            ):
                if key not in self._idle_packages:
                    line = self.layout.line
                    size = (self._cache[key][1] + line - 1) // line * line
                    self._idle_packages[key] = size
                    self._idle_bytes += size
                while self._idle_bytes > self.resident_bytes:
                    self._evict_package(next(iter(self._idle_packages)))
                return
            self._evict_package(key)

    def _evict_package(self, key):
        self._idle_bytes -= self._idle_packages.pop(key, 0)
        cached = self._cache.pop(key, None)
        self._prepared_keys.discard(key)
        if cached is None:
            return
        at, size = cached
        if key in self._spilled:
            del self._spilled[key]
            if self.unspill is not None:
                self.unspill(at)
        else:
            size += -size % self.layout.line
            spans = sorted([*self._heap_free, (at, size)])
            self._heap_free = []
            for lo, n in spans:
                if self._heap_free and sum(self._heap_free[-1]) == lo:
                    start, length = self._heap_free[-1]
                    self._heap_free[-1] = (start, length + n)
                else:
                    self._heap_free.append((lo, n))

    def _unspill_all(self) -> None:
        for key, at in list(self._spilled.items()):
            if key in self._package_tags.values() or self._package_pins.get(key):
                continue
            self._cache.pop(key, None)
            self._idle_bytes -= self._idle_packages.pop(key, 0)
            if self.unspill is not None:
                self.unspill(at)
            del self._spilled[key]

    def _drain_all(self) -> None:
        """Poll until every submitted entry has its final completion, at most
        the queue's `timeout` (then :class:`TimeoutError`)."""
        start = self._start()
        while self._outstanding - set(self._done):
            if self._waited(start, "a drain") > self.timeout:
                self.poll()
                if not self._outstanding - set(self._done):
                    break
                raise TimeoutError(
                    f"tags {sorted(self._outstanding - set(self._done))} did not "
                    f"complete in {self.timeout}s: {self.state()}"
                )
            if not self.poll():
                self._idle()

    def _start(self) -> tuple[float, float]:
        """A wait's start: (the queue's clock, wall seconds)."""
        return self.clock(), time.monotonic()

    def _waited(self, start: tuple[float, float], what: str) -> float:
        """Seconds on the queue's clock since `start`. Raises
        :class:`OverBudget` once the wait has run `alarm` wall seconds."""
        if self.alarm and time.monotonic() - start[1] > self.alarm:
            raise OverBudget(what, self.state(), self.alarm)
        return self.clock() - start[0]

    def _idle(self) -> None:
        if self.idle is not None:
            self.idle()
        elif self.poll_seconds:
            time.sleep(self.poll_seconds)

    # ----------------------------------------------------------------- stdio
    def _stdout(self, so_wr: int | None = None) -> None:
        wr = (
            int(self.mem.read64(self.base + self.layout.so_wr))
            if so_wr is None
            else so_wr
        )
        n = wr - self.so_rd
        if n <= 0:
            return
        n = min(n, self.so_bytes)
        start = self.so_rd % self.so_bytes
        lo = start & ~7
        span = -(-(start + n - lo) // 8) * 8
        ring = self.base + self.geo["so"]
        if lo + span <= self.so_bytes:
            raw = self.mem.read_block(ring + lo, span)
        else:
            first = self.so_bytes - lo
            raw = self.mem.read_block(ring + lo, first) + self.mem.read_block(
                ring, span - first
            )
        text = raw[start - lo : start - lo + n].decode("utf-8", "replace")
        self.console += text
        self.so_rd = wr
        self.mem.write_block(
            self.base + self.layout.so_rd, self.layout.line_bytes(self.so_rd)
        )

    def read_stdout(self) -> str:
        """Everything the firmware printed since the last call."""
        with self._native_lock:
            self._stdout()
            out, self.console = self.console, ""
            self.console_at += len(out)
            return out

    def read_stdout_to(self, end: int) -> str:
        """Read through a record's stdout count, keeping later ASCII text."""
        with self._native_lock:
            if self.console_at + len(self.console) < end:
                self._stdout()
            take = max(0, end - self.console_at)
            out, self.console = self.console[:take], self.console[take:]
            self.console_at += len(out)
            return out

    def stdin(self, text: str) -> None:
        """Push bytes into the firmware's stdin ring, whole lines at a time."""
        with self._native_lock:
            ring = self.base + self.geo["si"]
            touched = set()
            for ch in text.encode():
                at = self.si_wr % self.si_bytes
                self._stdin[at] = ch
                touched.add(at & ~(self.layout.line - 1))
                self.si_wr += 1
            for line_at in sorted(touched):
                self.mem.write_block(
                    ring + line_at,
                    bytes(self._stdin[line_at : line_at + self.layout.line]),
                )
            self.mem.write_block(
                self.base + self.layout.si_wr,
                self.layout.line_bytes(self.si_wr),
            )
