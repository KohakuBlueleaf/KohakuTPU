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

import hashlib
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass

from kohakuaccel.driver.device import mover as MV
from kohakuaccel.driver.node.queue import layout as L

#: The default region: rings, then a package heap.
REGION_BYTES = 1 << 20
#: Bindings per submission slot: the firmware's KA_MAX_BUFFERS.
MAX_BINDINGS = 64


@dataclass
class Completion:
    """One CQ entry, decoded."""

    tag: int
    status: int
    detail: int
    step: int
    cycles: int
    value: int

    @property
    def ok(self) -> bool:
        return self.status == L.OK

    @property
    def name(self) -> str:
        return L.STATUS.get(self.status, f"{self.status:#x}")

    def describe(self) -> str:
        what = self.name
        if self.status == 0x30:
            what += f" waiting on {L.WAITS.get(self.detail, self.detail)}"
        elif self.status == 0x20:
            what += f" from unit {self.detail}, signal word {self.value:#x}"
        elif self.status == 0x22:
            what += f" {self.detail}: {MV.FAULTS.get(self.detail, 'unknown')}"
        return f"tag {self.tag:#x}: {what} at step {self.step} ({self.cycles} cycles)"


class NodeError(RuntimeError):
    """A package the node refused or failed, with its completion."""

    def __init__(self, completion: Completion, console: str = "") -> None:
        self.completion = completion
        tail = f"\nnode said:\n{console.rstrip()}" if console.strip() else ""
        super().__init__(completion.describe() + tail)


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
    ) -> None:
        if base % 4096:
            raise ValueError(f"a queue region is page aligned; {base:#x} is not")
        self.mem = mem
        self.base = base
        self.size = size
        self.sq_n, self.cq_n = sq_n, cq_n
        self.so_bytes, self.si_bytes = so_bytes, si_bytes
        self.geo = L.geometry(sq_n, cq_n, so_bytes, si_bytes)
        #: Binding slots, one per SQ entry, then the package heap.
        self.bind_off = -(-self.geo["end"] // L.LINE) * L.LINE
        self.heap_off = -(-(self.bind_off + sq_n * MAX_BINDINGS * 8) // 4096) * 4096
        if self.heap_off >= size:
            raise ValueError(f"{size:#x} bytes leaves no package heap")
        self.idle = idle
        self.poll_seconds = poll_seconds
        #: `spill(nbytes) -> address` places a package larger than the heap
        #: anywhere the node reads (an entry carries the package's address).
        self.spill = spill
        self.sq_tail = self.cq_head = self.so_rd = self.si_wr = 0
        self._cache: OrderedDict[bytes, tuple[int, int]] = OrderedDict()
        self._heap_top = self.heap_off
        self._done: dict[int, Completion] = {}
        self._outstanding: set[int] = set()
        self._stdin = bytearray(si_bytes)
        self.progress: list[Completion] = []
        self._tag = 0
        self.console = ""
        self.counters = dict.fromkeys(
            ("submitted", "uploads", "upload_bytes", "cache_hits", "polls"), 0
        )

    # ----------------------------------------------------------------- setup
    def init(self) -> None:
        """Write the region's header and zero every index. Before the firmware boots."""
        info = L.line(
            L.MAGIC,
            L.VERSION,
            self.sq_n | self.cq_n << 16,
            self.so_bytes | self.si_bytes << 32,
        )
        self.mem.write_block(self.base, info + bytes(L.SQ - L.LINE))
        self.sq_tail = self.cq_head = self.so_rd = self.si_wr = 0
        self._cache.clear()
        self._heap_top = self.heap_off
        self._done.clear()
        self._outstanding.clear()
        self._stdin = bytearray(self.si_bytes)

    def state(self) -> dict:
        """The firmware's line: state, ABI, packages done, fatal code."""
        st, abi, done, code = struct.unpack(
            "<4Q", self.mem.read_block(self.base + L.FW, 32)
        )
        return {"state": L.STATES.get(st, st), "abi": abi, "done": done, "fatal": code}

    def units(self) -> list[tuple[str, int, int, int]]:
        """The units the firmware serves -- given at boot or found itself --
        as ``(type name, mesh, x, y)``. Published before it reports ready."""
        n = self.mem.read64(self.base + L.UNITS)
        raw = (
            self.mem.read_block(
                self.base + L.UNITS + L.LINE, -(-n * 8 // L.LINE) * L.LINE
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

    def wait_ready(self, timeout: float = 120.0) -> dict:
        """Poll until the firmware reports READY. Raises :class:`TimeoutError`."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            st = self.state()
            if st["state"] == "ready":
                self._stdout()
                return st
            if st["state"] == "fatal":
                raise RuntimeError(
                    f"firmware stopped fatally: {st}\n{self.read_stdout()}"
                )
            self._idle()
        raise TimeoutError(f"firmware never reported ready: {self.state()}")

    # ----------------------------------------------------------------- heap
    def upload(self, package: bytes) -> int:
        """Where `package` is in the heap, uploading it the first time.

        A package larger than the heap goes where `spill` puts it, or raises
        :class:`ValueError` without one. A full heap is emptied only when
        nothing submitted is still outstanding.
        """
        if len(package) % L.LINE:
            raise ValueError("a package is whole 32-byte lines")
        key = hashlib.sha1(package).digest()
        got = self._cache.get(key)
        if got is not None:
            self._cache.move_to_end(key)
            self.counters["cache_hits"] += 1
            return got[0]
        if self.heap_off + len(package) > self.size and self.spill is not None:
            at = self.spill(len(package))
            self.mem.write_block(at, package)
            self._cache[key] = (at, len(package))
            self.counters["uploads"] += 1
            self.counters["upload_bytes"] += len(package)
            return at
        if self.heap_off + len(package) > self.size:
            raise ValueError(
                f"a {len(package)}-byte package exceeds the {self.size - self.heap_off}-byte heap"
            )
        if self._heap_top + len(package) > self.size:
            self._drain_all()
            self._cache.clear()
            self._heap_top = self.heap_off
        at = self.base + self._heap_top
        self.mem.write_block(at, package)
        self._heap_top += -(-len(package) // L.LINE) * L.LINE
        self._cache[key] = (at, len(package))
        self.counters["uploads"] += 1
        self.counters["upload_bytes"] += len(package)
        return at

    # --------------------------------------------------------------- submit
    def submit(
        self,
        package: bytes | None = None,
        bindings=None,
        op: int = L.OP_RUN,
        timeout_cycles: int = 0,
        flags: int = 0,
        arg: int = 0,
        words=None,
    ) -> int:
        """Queue one entry; returns its tag. Waits while the SQ is full.

        `words` replaces entry words 2.. whole (at most 6), for the opcodes
        whose arguments are not a package's (HEAP, ALLOC, FREE).
        """
        while self.sq_tail - self._sq_head() >= self.sq_n:
            self.poll()
            self._idle()
        slot = self.sq_tail % self.sq_n
        self._tag += 1
        tag = self._tag
        pkg, nbytes, bind, nbind = arg, 0, 0, 0
        if package is not None:
            pkg, nbytes = self.upload(package), len(package)
        if bindings:
            nbind = len(bindings)
            if nbind > MAX_BINDINGS:
                raise ValueError(f"{nbind} bindings; the firmware binds {MAX_BINDINGS}")
            blob = struct.pack(f"<{nbind}Q", *bindings)
            blob += bytes(-len(blob) % L.LINE)
            bind = self.base + self.bind_off + slot * MAX_BINDINGS * 8
            self.mem.write_block(bind, blob)
        tail = [pkg, nbytes, bind, timeout_cycles, flags, 0]
        if words is not None:
            if len(words) > 6:
                raise ValueError(f"{len(words)} argument words; an entry carries 6")
            tail = list(words) + [0] * (6 - len(words))
        entry = struct.pack("<8Q", op | nbind << 16, tag, *tail)
        self.mem.write_block(self.base + self.geo["sq"] + slot * L.SQ_BYTES, entry)
        self.sq_tail += 1
        self.mem.write_block(self.base + L.SQ_TAIL, L.line(self.sq_tail))
        self.counters["submitted"] += 1
        self._outstanding.add(tag)
        return tag

    def _sq_head(self) -> int:
        return self.mem.read64(self.base + L.SQ_HEAD)

    # ----------------------------------------------------------------- poll
    def poll(self) -> list[Completion]:
        """Read every completion that has arrived; returns them in order."""
        self.counters["polls"] += 1
        raw = self.mem.read_block(self.base + L.CQ_TAIL, 3 * L.LINE)
        tail = struct.unpack_from("<Q", raw, 0)[0]
        so_wr = struct.unpack_from("<Q", raw, 2 * L.LINE)[0]
        if so_wr != self.so_rd:
            self._stdout(so_wr)
        got = []
        if tail != self.cq_head:
            n = tail - self.cq_head
            for k in range(n):
                at = (self.cq_head + k) % self.cq_n
                e = self.mem.read_block(
                    self.base + self.geo["cq"] + at * L.CQ_BYTES, L.CQ_BYTES
                )
                tag, word, cycles, value = struct.unpack("<4Q", e)
                c = Completion(
                    tag, word & 0xFFFF, (word >> 16) & 0xFFFF, word >> 32, cycles, value
                )
                got.append(c)
                if c.status == L.PROGRESS:
                    self.progress.append(c)
                else:
                    self._done[c.tag] = c
            self.cq_head = tail
            self.mem.write_block(self.base + L.CQ_HEAD, L.line(self.cq_head))
        return got

    def wait(self, tag: int, timeout: float = 300.0, check: bool = True) -> Completion:
        """Poll until `tag` completes. Raises :class:`NodeError` on a failed
        completion (unless `check` is False) and :class:`TimeoutError`."""
        t0 = time.monotonic()
        while tag not in self._done:
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(
                    f"tag {tag:#x} did not complete in {timeout}s: {self.state()}"
                )
            if not self.poll() and tag not in self._done:
                self._idle()
        c = self._done.pop(tag)
        self._outstanding.discard(tag)
        if check and not c.ok:
            raise NodeError(c, self.read_stdout())
        return c

    def take(self, tag: int) -> Completion | None:
        """`tag`'s final completion if it has arrived (one poll), else None:
        :meth:`wait` for a caller that owns the polling loop."""
        if tag not in self._done:
            self.poll()
        c = self._done.pop(tag, None)
        if c is not None:
            self._outstanding.discard(tag)
        return c

    def run(self, package: bytes, bindings=None, **kw) -> Completion:
        """Submit and wait."""
        timeout = kw.pop("timeout", 300.0)
        return self.wait(self.submit(package, bindings, **kw), timeout=timeout)

    def nop(self) -> Completion:
        return self.wait(self.submit(None, op=L.OP_NOP))

    def stop(self, value: int = 0) -> Completion:
        """Ask the firmware to leave its loop; it exits with STOP | value."""
        return self.wait(self.submit(None, op=L.OP_STOP, arg=value))

    # --------------------------------------------------------- node heaps
    def heap(
        self, region: int, base: int, nbytes: int, granule: int = 64
    ) -> Completion:
        """Cover node heap `region` with ``[base, base + nbytes)``; 0 bytes
        retires it. Raises :class:`NodeError` (BAD_ARG, HEAP_BUSY)."""
        return self.wait(
            self.submit(op=L.OP_HEAP, words=[region, base, nbytes, granule])
        )

    def alloc(self, region: int, nbytes: int, align: int = 0, tag: int = 0) -> int:
        """A block of `nbytes` (rounded up to the granule) from node heap
        `region`; returns its unit-global address. Raises :class:`NodeError`
        (NO_MEMORY, NO_SLOTS, BAD_ARG)."""
        c = self.wait(self.submit(op=L.OP_ALLOC, words=[region, nbytes, align, tag]))
        return c.value

    def free(self, region: int, addr: int) -> Completion:
        """Return a block to node heap `region`. Raises :class:`NodeError` (BAD_FREE)."""
        return self.wait(self.submit(op=L.OP_FREE, words=[region, addr]))

    def heap_stats(self, region: int) -> dict:
        """The firmware's line for `region`: free and largest free bytes, live
        blocks, table entries, failed requests (all 0 while unconfigured)."""
        free, largest, lb, fails = struct.unpack(
            "<4Q", self.mem.read_block(self.base + L.HEAPS + region * L.LINE, L.LINE)
        )
        return {
            "free": free,
            "largest": largest,
            "live": lb & 0xFFFF_FFFF,
            "blocks": lb >> 32,
            "fails": fails,
        }

    def _drain_all(self) -> None:
        """Poll until every submitted entry has its final completion."""
        while self._outstanding - set(self._done):
            if not self.poll():
                self._idle()

    def _idle(self) -> None:
        if self.idle is not None:
            self.idle()
        elif self.poll_seconds:
            time.sleep(self.poll_seconds)

    # ----------------------------------------------------------------- stdio
    def _stdout(self, so_wr: int | None = None) -> None:
        if so_wr is None:
            so_wr = self.mem.read64(self.base + L.SO_WR)
        n = so_wr - self.so_rd
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
        self.so_rd = so_wr
        self.mem.write_block(self.base + L.SO_RD, L.line(self.so_rd))

    def read_stdout(self) -> str:
        """Everything the firmware printed since the last call."""
        self._stdout()
        out, self.console = self.console, ""
        return out

    def stdin(self, text: str) -> None:
        """Push bytes into the firmware's stdin ring, whole lines at a time."""
        ring = self.base + self.geo["si"]
        touched = set()
        for ch in text.encode():
            at = self.si_wr % self.si_bytes
            self._stdin[at] = ch
            touched.add(at & ~(L.LINE - 1))
            self.si_wr += 1
        for line_at in sorted(touched):
            self.mem.write_block(
                ring + line_at, bytes(self._stdin[line_at : line_at + L.LINE])
            )
        self.mem.write_block(self.base + L.SI_WR, L.line(self.si_wr))
