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
        record: L.Record | dict | None = None,
        *,
        layout: L.Layout,
    ) -> None:
        self.m, self.q = mem, base
        self.interp = interp
        self.ahead = ahead
        self.next = None
        self.events: list[tuple] = []
        self.record = record
        self.layout = layout
        self.epoch = 0
        self.list_index = 0
        self.item_tag = 0

    def rd(self, off):
        return self.m.read64(self.q + off)

    def wr(self, off, v):
        self.m.write64(self.q + off, v)

    def _fatal(self, code: int) -> None:
        layout = self.layout
        self.wr(layout.fw + 8 * layout.fw_abi, layout.version)
        self.wr(layout.fw + 8 * layout.fw_done, 0)
        self.wr(layout.fw + 8 * layout.fw_fatal, code)
        self.wr(layout.list_abi, layout.list_version)
        self.wr(layout.fw + 8 * layout.fw_state, layout.st_fatal)

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
        layout = self.layout
        if args is not None:
            bad = self.check_boot(args)
            if bad:
                self._fatal(B.EXIT_BOOTARGS | bad)
                return bad
        if self.rd(layout.info + 8 * layout.info_words["magic"]) != layout.magic:
            self._fatal(B.EXIT_BOOTARGS | B.ERR_QUEUE)
            return B.ERR_QUEUE
        g = self.rd(layout.info + 8 * layout.info_words["rings"])
        s = self.rd(layout.info + 8 * layout.info_words["stdio"])
        self.sq_n, self.cq_n, self.so, self.si = (
            g & 0xFFFF,
            g >> 16,
            s & layout.stdio_mask,
            s >> 32,
        )
        if (
            not self.sq_n
            or not self.cq_n
            or not self.so
            or not self.si
            or self.so % layout.line
            or self.si % layout.line
        ):
            self._fatal(B.EXIT_BOOTARGS | B.ERR_QUEUE)
            return B.ERR_QUEUE
        self.sq = self.q + layout.sq
        self.cq = self.sq + layout.sq_bytes * self.sq_n
        self.so_at = (
            -(-(self.cq + layout.cq_bytes * self.cq_n) // layout.line) * layout.line
        )
        self.si_at = self.so_at + self.so
        self.rec_at = self.si_at + self.si
        self.wr(layout.fw + 8 * layout.fw_abi, layout.version)
        self.epoch = self.rd(layout.epoch)
        self.wr(layout.list_abi, layout.list_version)
        self.wr(layout.fw + 8 * layout.fw_state, layout.st_ready)
        return 0

    def print(self, text: str):
        wr = self.rd(self.layout.so_wr)
        for ch in text.encode():
            at = wr % self.so
            word = self.m.read64(self.so_at + (at & ~7))
            word = (word & ~(0xFF << 8 * (at & 7))) | ch << 8 * (at & 7)
            self.m.write64(self.so_at + (at & ~7), word)
            wr += 1
        self.wr(self.layout.so_wr, wr)

    def entry(self, i):
        return [
            self.m.read64(self.sq + (i % self.sq_n) * self.layout.sq_bytes + 8 * k)
            for k in range(self.layout.sq_words)
        ]

    def read_run(self, e):
        """A RUN entry's package bytes and bindings."""
        layout = self.layout
        nbind = (e[layout.run_op] >> layout.nbind_shift) & 0xFFFF
        pkg = self.m.read_block(e[layout.run_pkg], e[layout.run_bytes])
        binds = (
            [self.m.read64(e[layout.run_bind] + 8 * k) for k in range(nbind)]
            if e[layout.run_bind]
            else []
        )
        return pkg, binds

    def step(self):
        layout = self.layout
        head, tail = self.rd(layout.sq_head), self.rd(layout.sq_tail)
        if head == tail:
            return
        ahead = self.next is not None and self.next[0] == head
        e = self.next[1] if ahead else self.entry(head)
        self.next = None
        op = e[layout.run_op] & layout.op_mask
        self.epoch = self.rd(layout.epoch)
        self.list_index = self.item_tag = 0
        status, detail, step_idx, value, time = layout.ok, 0, 0, 0, {}
        if op == layout.op_run:
            pkg, binds = self.read_run(e)
            self.events.append(("start", head, ahead))
            self.print(f"run {len(pkg)}B {binds}\n")
            if self.interp:
                res = self.interp.run(pkg, binds)
                status, value = res.status, res.sent
                if self.record is not None:
                    time = res.time
                if status != layout.ok:
                    self.print(
                        f"[ka] package {e[layout.run_tag]:x} failed: status {status:x} "
                        f"detail {detail:x} step {step_idx} value {value:x}\n"
                    )
            # Read only entry words; package bytes remain mutable until execution.
            nxt = head + 1
            if self.ahead and status == layout.ok and nxt != self.rd(layout.sq_tail):
                self.next = (nxt, self.entry(nxt))
                self.events.append(("ahead", nxt, head))
        elif op == layout.op_list:
            epoch = e[layout.list_epoch]
            if (
                epoch != self.epoch
                or e[layout.list_version_word] != layout.list_version
                or e[layout.list_flags]
            ):
                status = layout.bad_arg
                detail = 1 if epoch != self.epoch else 2
                value = epoch
            else:
                for i in range(e[layout.list_count]):
                    item = [
                        self.m.read64(e[layout.list_addr] + i * layout.sq_bytes + 8 * k)
                        for k in range(layout.sq_words)
                    ]
                    self.list_index, self.item_tag = i, item[layout.run_tag]
                    item_op = item[layout.run_op] & layout.op_mask
                    if item_op != layout.op_run:
                        status, detail, step_idx, value = layout.bad_op, item_op, 0, 0
                    else:
                        pkg, binds = self.read_run(item)
                        self.events.append(("list-item", i, self.item_tag))
                        if self.interp:
                            res = self.interp.run(pkg, binds)
                            status = res.status
                            if status != layout.ok:
                                value = res.sent
                            else:
                                value += res.sent
                            if self.record is not None:
                                for key, cycles in res.time.items():
                                    time[key] = time.get(key, 0) + cycles
                    if status != layout.ok:
                        self.print(
                            f"[ka] package {self.item_tag:x} failed: status {status:x} "
                            f"detail {detail:x} step {step_idx} value {value:x}\n"
                        )
                        break
                    self.list_index = i + 1
        elif op not in (layout.op_nop, layout.op_stop):
            status, detail = layout.bad_op, op
        ct = self.rd(layout.cq_tail)
        assert ct - self.rd(layout.cq_head) < self.cq_n
        if self.record is not None:
            self.write_record(ct % self.cq_n, e[layout.run_tag], time)
        at = self.cq + (ct % self.cq_n) * layout.cq_bytes
        mask = layout.cq_status_mask
        word1 = (status & mask) | ((detail & mask) << 16) | (step_idx << 32)
        self.m.write64(at + layout.cq_tag, e[layout.run_tag])
        self.m.write64(at + layout.cq_status, word1)
        self.m.write64(at + layout.cq_cycles, 0)
        self.m.write64(at + layout.cq_value, value)
        self.wr(layout.cq_tail, ct + 1)
        self.events.append(("complete", head, status))
        self.wr(layout.sq_head, head + 1)

    def write_record(self, slot: int, tag: int, time: dict) -> None:
        """Write the run record before its CQ entry (L0.NQ.056)."""
        nbytes = (
            self.record.nbytes
            if isinstance(self.record, L.Record)
            else self.record["bytes"]
        )
        fields = (
            {k: off for k, (off, _) in self.record.fields.items()}
            if isinstance(self.record, L.Record)
            else self.record["fields"]
        )
        base = self.rec_at + slot * nbytes
        words = {
            "tag": tag,
            "so_wr": self.rd(self.layout.so_wr),
            "list_index": self.list_index,
            "item_tag": self.item_tag,
            "epoch": self.epoch,
            **time,
        }
        for name, off in fields.items():
            self.m.write64(base + off, words.get(name, 0))
