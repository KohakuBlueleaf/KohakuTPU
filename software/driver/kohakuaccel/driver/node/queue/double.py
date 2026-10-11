"""SpecFirmware: the firmware half of the node queue, from docs/spec/node-queue.md alone.

A test double, written independently of :class:`NodeQueue`, over any
Transport addressed as the queue is. `step()` serves one submission the way
the dispatcher does; a test calls it where a card would run. RUN entries are
echoed to stdout and, given an `interp` (anything with ``run(pkg, binds)``
returning ``.status`` and ``.sent``), run; HEAP / ALLOC / FREE follow the
node heaps' policy (kohakuaccel.driver.node.heap) and publish the region's
line.
"""

from kohakuaccel.driver.node.heap import Heap
from kohakuaccel.driver.node.queue import layout as L


class SpecFirmware:
    """The firmware half of the queue protocol, from the spec alone."""

    def __init__(self, mem, base: int, interp=None) -> None:
        self.m, self.q = mem, base
        self.interp = interp
        self.heaps: dict[int, Heap] = {}

    def rd(self, off):
        return self.m.read64(self.q + off)

    def wr(self, off, v):
        self.m.write64(self.q + off, v)

    def boot(self):
        assert self.rd(L.INFO) == L.MAGIC
        g, s = self.rd(L.INFO + 16), self.rd(L.INFO + 24)
        self.sq_n, self.cq_n, self.so, self.si = (
            g & 0xFFFF,
            g >> 16,
            s & 0xFFFF_FFFF,
            s >> 32,
        )
        self.sq = self.q + L.SQ
        self.cq = self.sq + 64 * self.sq_n
        self.so_at = self.cq + 32 * self.cq_n
        self.si_at = self.so_at + self.so
        self.wr(L.FW, L.ST_READY)

    def print(self, text: str):
        wr = self.rd(L.SO_WR)
        for ch in text.encode():
            at = wr % self.so
            word = self.m.read64(self.so_at + (at & ~7))
            word = (word & ~(0xFF << 8 * (at & 7))) | ch << 8 * (at & 7)
            self.m.write64(self.so_at + (at & ~7), word)
            wr += 1
        self.wr(L.SO_WR, wr)

    def step(self):
        head, tail = self.rd(L.SQ_HEAD), self.rd(L.SQ_TAIL)
        if head == tail:
            return
        e = [self.m.read64(self.sq + (head % self.sq_n) * 64 + 8 * k) for k in range(8)]
        op, nbind = e[0] & 0xFF, (e[0] >> 16) & 0xFFFF
        status, value = L.OK, 0
        if op == L.OP_RUN:
            pkg = self.m.read_block(e[2], e[3])
            binds = [self.m.read64(e[4] + 8 * k) for k in range(nbind)] if e[4] else []
            self.print(f"run {len(pkg)}B {binds}\n")
            if self.interp:
                res = self.interp.run(pkg, binds)
                status, value = res.status, res.sent
        elif op in (L.OP_HEAP, L.OP_ALLOC, L.OP_FREE):
            status, value = self.heap_op(op, e)
        elif op not in (L.OP_NOP, L.OP_STOP):
            status = 0x40
        ct = self.rd(L.CQ_TAIL)
        assert ct - self.rd(L.CQ_HEAD) < self.cq_n
        at = self.cq + (ct % self.cq_n) * 32
        for k, v in enumerate((e[1], status, 0, value)):
            self.m.write64(at + 8 * k, v)
        self.wr(L.CQ_TAIL, ct + 1)
        self.wr(L.SQ_HEAD, head + 1)

    def heap_op(self, op, e):
        """s3.1: HEAP region, base, bytes, granule; ALLOC region, bytes, align,
        tag; FREE region, address. Then the region's line (s3)."""
        r = e[2]
        if r >= L.MEM_REGIONS:
            return L.BAD_ARG, 0
        h, status, value = self.heaps.get(r), L.OK, 0
        if op == L.OP_HEAP:
            if h is not None and h.live:
                status = L.HEAP_BUSY
            elif e[4] == 0:
                self.heaps.pop(r, None)
            else:
                h = Heap(e[3], e[4], e[5] or 64, 64)
                status = h.rc
                if status == L.OK:
                    self.heaps[r] = h
                else:
                    self.heaps.pop(r, None)
        elif h is None:
            status = L.BAD_ARG
        elif op == L.OP_ALLOC:
            status, value = h.alloc(e[3], e[4], e[5])
        else:
            status = h.free(e[3])
        h = self.heaps.get(r)
        st = (
            h.stats()
            if h
            else dict.fromkeys(("free", "largest", "live", "blocks", "fails"), 0)
        )
        line = (st["free"], st["largest"], st["live"] | st["blocks"] << 32, st["fails"])
        for k, v in enumerate(line):
            self.wr(L.HEAPS + 32 * r + 8 * k, v)
        return status, value
