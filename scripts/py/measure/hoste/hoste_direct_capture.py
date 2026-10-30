"""File-backed native queue transcript generation; no arithmetic in this server."""

import os
import struct
import threading
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from hoste_direct import FileQueue


@contextmanager
def capture_direct(dev, callbacks=False):
    backend = FileQueue(dev)
    layout = backend.layout
    stop = threading.Event()

    def firmware():
        head = 0
        while not stop.is_set():
            raw = os.pread(backend.fd, 8, layout.sq_tail)
            if len(raw) != 8 or int.from_bytes(raw, "little") == head:
                time.sleep(0.0001)
                continue
            entry = struct.unpack(
                "<8Q",
                os.pread(
                    backend.fd, layout.sq_bytes, layout.sq + head % 4 * layout.sq_bytes
                ),
            )
            tag = entry[layout.run_tag]
            os.pwrite(
                backend.fd,
                struct.pack("<4Q", tag, 0, 0, 0),
                backend.geo["cq"] + head % 4 * layout.cq_bytes,
            )
            os.pwrite(
                backend.fd,
                layout.line_bytes(tag) + bytes(64),
                backend.geo["rec"] + head % 4 * layout.rec_bytes,
            )
            head += 1
            os.pwrite(backend.fd, layout.line_bytes(head), layout.sq_head)
            os.pwrite(backend.fd, layout.line_bytes(head), layout.cq_tail)

    worker = threading.Thread(target=firmware)
    worker.start()

    def callback(raw, debug):
        return backend.endpoint.submit(dev.card, raw[0][0])

    try:
        with ExitStack() as stack:
            if callbacks:
                stack.enter_context(patch.object(dev.card, "submit_wire", callback))
            else:
                stack.enter_context(
                    patch.object(
                        dev.card, "native_direct", backend.endpoint, create=True
                    )
                )
            yield backend
    finally:
        stop.set()
        worker.join()
        backend.file.close()
