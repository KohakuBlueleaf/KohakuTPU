"""Native bounded queue polling over the XDMA transport's current file handle."""

import os

from kohakutpu.compiler import _rs


class NativePoll:
    def __init__(self, queue, transport, layout=None):
        self.queue = queue
        self.transport = transport
        self.layout = _rs.QueueLayout(queue.layout) if layout is None else layout
        self.span = self.layout.so_wr + self.layout.line - self.layout.fw
        self.address = queue.mem._map(queue.base + self.layout.fw, self.span)
        self.sleep = float(os.environ.get("KTPU_NATIVE_POLL_SLEEP", "0"))
        self.result = None

    def capture(self, address, nbytes):
        self.result = _rs.wait_tail(
            self.transport._c2h,
            address,
            self.queue.cq_head,
            self.queue.so_rd,
            self.seconds,
            self.sleep,
            0,
            self.layout,
        )

    def __call__(self, seconds):
        self.seconds = seconds
        self.transport._retry("c2h", self.address, self.span, self.capture)
        raw, polls = self.result
        self.transport.counts["c2h"] += polls
        self.transport.counts["c2h_bytes"] += polls * len(raw)
        return raw, polls
