"""SpecFirmware: the firmware half of the node queue, from docs/spec/node-queue.md alone.

A test double, written independently of :class:`NodeQueue`, over any
Transport addressed as the queue is. `step()` serves one submission the way
the dispatcher does; a test calls it where a card would run. RUN entries are
echoed to stdout and, given an `interp` (anything with ``run(pkg, binds)``
returning ``.status`` and ``.sent``), run. Memory allocation is host-only;
reserved opcodes refuse with BAD_OP.
"""

import struct

from kohakuaccel.driver.node.boot import args as B
from kohakuaccel.driver.node.queue import layout as L


class SpecFirmware:
    """The firmware half of the queue protocol, from the spec alone."""

    def __init__(
        self,
        mem,
        base: int,
        interp=None,
        ahead: bool = True,
        record: dict | None = None,
    ) -> None:
        self.m, self.q = mem, base
        self.interp = interp
        self.ahead = ahead
        self.next = None
        self.events: list[tuple] = []
        self.record = record

    def rd(self, off):
        return self.m.read64(self.q + off)

    def wr(self, off, v):
        self.m.write64(self.q + off, v)

    def _fatal(self, code: int) -> None:
        self.wr(L.FW + 8, L.VERSION)
        self.wr(L.FW + 16, 0)
        self.wr(L.FW + 24, code)
        self.wr(L.FW, L.ST_FATAL)

    @staticmethod
    def check_boot(raw: bytes) -> int:
        """`ka_boot_check` over a packed boot block: 0 or `ERR_*`."""
        if len(raw) < B.WORDS * 8:
            return B.ERR_MAGIC
        w = struct.unpack_from(f"<{B.WORDS}Q", raw)
        if w[0] != B.MAGIC:
            return B.ERR_MAGIC
        if w[1] != B.VERSION:
            return B.ERR_VERSION
        if w[8] > B.MAX_UNITS:
            return B.ERR_UNITS
        at = 9 + B.MAX_UNITS
        regions = [w[at + 3 * i : at + 3 * (i + 1)] for i in range(B.REGIONS)]
        for i, (base, size, use) in enumerate(regions):
            if not use:
                continue
            if not size or base + size >= 1 << 64:
                return B.ERR_REGIONS
            for other, nbytes, kind in regions[:i]:
                if not kind:
                    continue
                if use == kind or (base < other + nbytes and other < base + size):
                    return B.ERR_REGIONS
        return 0

    def boot(self, args: bytes | None = None) -> int:
        if args is not None:
            bad = self.check_boot(args)
            if bad:
                self._fatal(B.EXIT_BOOTARGS | bad)
                return bad
        if self.rd(L.INFO) != L.MAGIC:
            self._fatal(B.EXIT_BOOTARGS | B.ERR_QUEUE)
            return B.ERR_QUEUE
        g, s = self.rd(L.INFO + 16), self.rd(L.INFO + 24)
        self.sq_n, self.cq_n, self.so, self.si = (
            g & 0xFFFF,
            g >> 16,
            s & 0xFFFF_FFFF,
            s >> 32,
        )
        if (
            not self.sq_n
            or not self.cq_n
            or not self.so
            or not self.si
            or self.so % L.LINE
            or self.si % L.LINE
        ):
            self._fatal(B.EXIT_BOOTARGS | B.ERR_QUEUE)
            return B.ERR_QUEUE
        self.sq = self.q + L.SQ
        self.cq = self.sq + 64 * self.sq_n
        self.so_at = -(-(self.cq + 32 * self.cq_n) // L.LINE) * L.LINE
        self.si_at = self.so_at + self.so
        self.rec_at = self.si_at + self.si
        self.wr(L.FW + 8, L.VERSION)
        self.wr(L.FW, L.ST_READY)
        return 0

    def print(self, text: str):
        wr = self.rd(L.SO_WR)
        for ch in text.encode():
            at = wr % self.so
            word = self.m.read64(self.so_at + (at & ~7))
            word = (word & ~(0xFF << 8 * (at & 7))) | ch << 8 * (at & 7)
            self.m.write64(self.so_at + (at & ~7), word)
            wr += 1
        self.wr(L.SO_WR, wr)

    def entry(self, i):
        return [self.m.read64(self.sq + (i % self.sq_n) * 64 + 8 * k) for k in range(8)]

    def read_run(self, e):
        """A RUN entry's package bytes and bindings."""
        nbind = (e[0] >> 16) & 0xFFFF
        pkg = self.m.read_block(e[2], e[3])
        binds = [self.m.read64(e[4] + 8 * k) for k in range(nbind)] if e[4] else []
        return pkg, binds

    def step(self):
        head, tail = self.rd(L.SQ_HEAD), self.rd(L.SQ_TAIL)
        if head == tail:
            return
        ahead = self.next is not None and self.next[0] == head
        e = self.next[1] if ahead else self.entry(head)
        self.next = None
        op = e[0] & 0xFF
        status, value, time = L.OK, 0, {}
        if op == L.OP_RUN:
            pkg, binds = self.read_run(e)
            self.events.append(("start", head, ahead))
            self.print(f"run {len(pkg)}B {binds}\n")
            if self.interp:
                res = self.interp.run(pkg, binds)
                status, value = res.status, res.sent
                time = getattr(res, "time", {})
            # Read only entry words; package bytes remain mutable until execution.
            nxt = head + 1
            if self.ahead and status == L.OK and nxt != self.rd(L.SQ_TAIL):
                self.next = (nxt, self.entry(nxt))
                self.events.append(("ahead", nxt, head))
        elif op not in (L.OP_NOP, L.OP_STOP):
            status = 0x40
        ct = self.rd(L.CQ_TAIL)
        assert ct - self.rd(L.CQ_HEAD) < self.cq_n
        if self.record is not None:
            self.write_record(ct % self.cq_n, e[1], time)
        at = self.cq + (ct % self.cq_n) * 32
        for k, v in enumerate((e[1], status, 0, value)):
            self.m.write64(at + 8 * k, v)
        self.wr(L.CQ_TAIL, ct + 1)
        self.events.append(("complete", head, status))
        self.wr(L.SQ_HEAD, head + 1)

    def write_record(self, slot: int, tag: int, time: dict) -> None:
        """Write the run record before its CQ entry (L0.NQ.056)."""
        base = self.rec_at + slot * self.record["bytes"]
        words = {"tag": tag, "so_wr": self.rd(L.SO_WR), **time}
        for name, off in self.record["fields"].items():
            self.m.write64(base + off, words.get(name, 0))
