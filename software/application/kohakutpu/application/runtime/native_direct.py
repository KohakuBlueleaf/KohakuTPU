"""Cold setup of synchronous native eager upload and queue ownership."""

from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.compiler import _rs


def attach(dev):
    card = dev.card
    if not isinstance(card.host, XdmaLinux) or not isinstance(
        dev.memory.dram, _rs.Arena
    ):
        raise TypeError("KTPU_NATIVE_DIRECT requires Linux XDMA and KTPU_NATIVE_MEMORY")
    if len(card.nodes) != 1:
        raise ValueError(
            "native direct eager currently requires one node; use ordinary multi-node queues"
        )
    queue = card.q
    if card.native_direct is not None:
        card.native_direct.close()
    card.native_direct = _rs.DirectWire(
        queue,
        card.host,
        card.memory,
        card.mem._map(queue.base, queue.geo["end"]),
        card.mem._map(card.memory.dram.lo) - card.memory.dram.lo,
        (queue.sq_n, queue.cq_n, queue.geo["cq"], queue.geo["rec"], queue.geo["so"]),
        queue.record.fields,
    )
