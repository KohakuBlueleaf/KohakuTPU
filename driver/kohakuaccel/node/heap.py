"""The node's region-heap policy in Python (firmware/kohakuaccel/os/heap/heap.c).

The same first-fit-by-address placement, granule rounding, alignment and
neighbour merging, so a host can predict where the node will place a block and
a test can check the C implementation operation by operation
(driver/tests/test_node_heap.py). The block table is a list of
``[off, len, used, tag]`` covering the heap exactly, in address order.
"""

from kohakuaccel.node.queue import layout as L


def _pow2(v: int) -> bool:
    return v > 0 and not v & (v - 1)


class Heap:
    """One region heap. Methods return the firmware's status codes."""

    def __init__(self, base: int, nbytes: int, granule: int, cap: int) -> None:
        self.rc = self._init(base, nbytes, granule, cap)

    def _init(self, base: int, nbytes: int, granule: int, cap: int) -> int:
        if not _pow2(granule) or granule < 8 or base % granule or cap < 3:
            return L.BAD_ARG
        nbytes -= nbytes % granule
        if not nbytes:
            return L.BAD_ARG
        self.base, self.bytes, self.granule, self.cap = base, nbytes, granule, cap
        self.blocks = [[0, nbytes, 0, 0]]
        self.used = self.peak = self.live = self.fails = 0
        return L.OK

    def alloc(self, nbytes: int, align: int = 0, tag: int = 0) -> tuple[int, int]:
        """``(status, address)``; the address is 0 unless the status is OK."""
        if not nbytes or (align and not _pow2(align)):
            return L.BAD_ARG, 0
        g = self.granule
        if nbytes > self.bytes:
            self.fails += 1
            return L.NO_MEMORY, 0
        size = -(-nbytes // g) * g
        a = max(align, g)
        for i, (off, ln, used, _) in enumerate(self.blocks):
            if used or ln < size:
                continue
            at = -(-(self.base + off) // a) * a - self.base
            pad = at - off
            if pad > ln or ln - pad < size:
                continue
            rest = ln - pad - size
            need = (pad != 0) + (rest != 0)
            if len(self.blocks) + need > self.cap:
                self.fails += 1
                return L.NO_SLOTS, 0
            new = []
            if pad:
                new.append([off, pad, 0, 0])
            new.append([at, size, 1, tag & 0xFFFF_FFFF])
            if rest:
                new.append([at + size, rest, 0, 0])
            self.blocks[i : i + 1] = new
            self.used += size
            self.live += 1
            self.peak = max(self.peak, self.used)
            return L.OK, self.base + at
        self.fails += 1
        return L.NO_MEMORY, 0

    def free(self, addr: int) -> int:
        off = addr - self.base
        i = next(
            (k for k, b in enumerate(self.blocks) if b[0] == off and b[2]),
            None,
        )
        if i is None or not 0 <= off < self.bytes:
            return L.BAD_FREE
        b = self.blocks[i]
        self.used -= b[1]
        self.live -= 1
        b[2] = b[3] = 0
        if i + 1 < len(self.blocks) and not self.blocks[i + 1][2]:
            b[1] += self.blocks.pop(i + 1)[1]
        if i > 0 and not self.blocks[i - 1][2]:
            self.blocks[i - 1][1] += self.blocks.pop(i)[1]
        return L.OK

    def stats(self) -> dict:
        free = [b[1] for b in self.blocks if not b[2]]
        return {
            "free": self.bytes - self.used,
            "largest": max(free, default=0),
            "live": self.live,
            "blocks": len(self.blocks),
            "fails": self.fails,
        }
