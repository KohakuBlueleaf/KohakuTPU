"""One AXI window served by two backends, chosen per access by size.

Registers and bulk data want opposite things from a link. A control write is one
word and wants low latency; a weight upload is megabytes and wants a DMA
descriptor. A machine that has both a JTAG master and a PCIe engine should use
each for what it is good at rather than picking one and paying for it everywhere.
"""

from kohakuaccel.transport.base import Transport, require_words


class Split(Transport):
    """Word access to `control`, blocks of `threshold` bytes or more to `bulk`.

    `threshold` defaults to `bulk.min_xfer` where the backend declares one, else
    4096. Both backends address the same window, so `base` belongs to them and
    not here. Raises whatever the chosen backend raises.
    """

    bulk = True

    def __init__(
        self, control: Transport, bulk: Transport, threshold: int | None = None
    ) -> None:
        self.control, self.bulk_transport = control, bulk
        if threshold is None:
            threshold = getattr(bulk, "min_xfer", 4096)
        self.threshold = threshold
        self.counts = dict.fromkeys(("words", "small", "large"), 0)

    def write64(self, addr: int, data: int) -> None:
        self.counts["words"] += 1
        self.control.write64(addr, data)

    def read64(self, addr: int) -> int:
        self.counts["words"] += 1
        return self.control.read64(addr)

    def _pick(self, nbytes: int) -> Transport:
        big = nbytes >= self.threshold
        self.counts["large" if big else "small"] += 1
        return self.bulk_transport if big else self.control

    def write_block(self, addr: int, data: bytes) -> None:
        require_words(len(data))
        self._pick(len(data)).write_block(addr, data)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        require_words(nbytes)
        return self._pick(nbytes).read_block(addr, nbytes)

    def close(self) -> None:
        """Close both backends, whichever of them has a close."""
        for t in (self.control, self.bulk_transport):
            closer = getattr(t, "close", None)
            if closer is not None:
                closer()

    def __repr__(self) -> str:
        return (
            f"Split(control={self.control!r}, bulk={self.bulk_transport!r}, "
            f"threshold={self.threshold}, {self.counts})"
        )
