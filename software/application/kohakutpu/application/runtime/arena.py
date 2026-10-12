"""A first-fit allocator over one address range, spans coalesced on free."""

import bisect


class OutOfMemory(MemoryError):
    """No free span is large enough."""


class Arena:
    """Spans of [lo, hi): `align`-aligned starts, sizes rounded to `align`."""

    def __init__(self, lo: int, hi: int, align: int = 256) -> None:
        if hi <= lo:
            raise ValueError(f"an empty arena [{lo:#x}, {hi:#x})")
        self.lo, self.hi, self.align = lo, hi, align
        start = -(-lo // align) * align
        self.free_list: list = [(start, hi - start)]  # sorted (addr, size)
        self.live: dict = {}  # addr -> size

    def alloc(self, nbytes: int) -> int:
        size = max(self.align, -(-int(nbytes) // self.align) * self.align)
        for i, (at, n) in enumerate(self.free_list):
            if n >= size:
                if n == size:
                    self.free_list.pop(i)
                else:
                    self.free_list[i] = (at + size, n - size)
                self.live[at] = size
                return at
        raise OutOfMemory(
            f"{nbytes:,} bytes do not fit: {self.free:,} free in "
            f"{len(self.free_list)} spans, the largest {self.largest:,}"
        )

    def release(self, addr: int) -> None:
        size = self.live.pop(addr)
        i = bisect.bisect_left(self.free_list, (addr, 0))
        self.free_list.insert(i, (addr, size))
        # Coalesce with the neighbours.
        if i + 1 < len(self.free_list):
            a, n = self.free_list[i]
            b, m = self.free_list[i + 1]
            if a + n == b:
                self.free_list[i : i + 2] = [(a, n + m)]
        if i > 0:
            a, n = self.free_list[i - 1]
            b, m = self.free_list[i]
            if a + n == b:
                self.free_list[i - 1 : i + 1] = [(a, n + m)]

    @property
    def free(self) -> int:
        return sum(n for _, n in self.free_list)

    @property
    def used(self) -> int:
        return sum(self.live.values())

    @property
    def largest(self) -> int:
        return max((n for _, n in self.free_list), default=0)


__all__ = ["Arena", "OutOfMemory"]
