"""Native whole-launch versus native preparation with Python publication/retirement.

Both modes use the same file-backed direct endpoint and ready synthetic CQ.
This isolates Python ownership overhead, not physical DMA latency or arithmetic.
"""

import argparse
import json
import os
import statistics
import struct
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.native_launch import descriptor
from kohakutpu.compiler import _rs


class FileQueue:
    def __init__(self, dev):
        # Owned through worker shutdown.
        self.file = tempfile.TemporaryFile()  # noqa: SIM115
        self.fd = self.file.fileno()
        self.layout = dev.card.queue_layout
        self.geo = self.layout.geometry(4, 4, 4096, 4096)
        os.pwrite(
            self.fd,
            self.layout.info_line(self.layout.version, 4, 4, 4096, 4096, 1),
            self.layout.info,
        )
        os.pwrite(self.fd, self.layout.line_bytes(self.layout.st_ready), self.layout.fw)
        self.queue = SimpleNamespace(
            layout=self.layout,
            _native_lock=threading.RLock(),
            timeout=5.0,
            version=self.layout.version,
            _generation=1,
            _broken=False,
            _outstanding=set(),
            _staged=[],
            sq_tail=0,
            cq_head=0,
            so_rd=0,
            _tag=0,
            console="",
            console_at=0,
            counters={"uploads": 0, "upload_bytes": 0, "submitted": 0, "polls": 0},
        )
        self.transport = SimpleNamespace(
            _c2h=self.fd, _h2c=self.fd, fd_generation=1, _native_lock=threading.RLock()
        )
        dev.card._publication_epoch = 0
        self.endpoint = _rs.DirectWire(
            self.queue,
            self.transport,
            dev.memory,
            0,
            0,
            (4, 4, self.geo["cq"], self.geo["rec"], self.geo["so"]),
            {},
        )

    def ready(self):
        q = self.queue
        layout = self.layout
        os.pwrite(
            self.fd,
            struct.pack("<4Q", q._tag + 1, 0, 123, 0),
            self.geo["cq"] + q.cq_head % 4 * layout.cq_bytes,
        )
        os.pwrite(
            self.fd,
            layout.line_bytes(q._tag + 1) + bytes(64),
            self.geo["rec"] + q.cq_head % 4 * layout.rec_bytes,
        )
        os.pwrite(self.fd, layout.line_bytes(q.cq_head + 1), layout.cq_tail)
        os.pwrite(self.fd, layout.line_bytes(q.sq_tail), layout.sq_head)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeat", type=int, default=1000)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    os.environ.setdefault("KTPU_NATIVE_MEMORY", "1")
    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    compiled = dev.specialise(kernel, given)
    names = compiled.l1.params(compiled.name, "l1")
    handles = [dev.alloc(4096) for _ in names]
    binds = dict(zip((name for name, _ in names), handles, strict=True))
    captured = []

    def capture(compiled, full, progs, submitted):
        captured.append(descriptor(dev, compiled, full, progs, submitted, binds))

    with patch.object(dev, "_recipe_capture", capture):
        dev.run(compiled, binds)
    native = captured[0]
    backend = FileQueue(dev)

    def callback(raw, debug):
        return backend.endpoint.submit(dev.card, raw[0][0])

    result = {}
    try:
        with patch.object(dev.card, "submit_wire", callback):
            for mode in (False, True):
                dev.card.native_direct = backend.endpoint if mode else None
                samples = []
                for _ in range(args.repeat):
                    backend.ready()
                    start = time.process_time_ns()
                    submitted = native.launch(dev, handles)
                    if not mode:
                        dev.collect_through(submitted)
                    samples.append((time.process_time_ns() - start) / 1e6)
                result[str(mode)] = {
                    "median_ms": statistics.median(samples[10:]),
                    "samples": samples,
                }
    finally:
        backend.file.close()
        dev.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({key: value["median_ms"] for key, value in result.items()}),
        flush=True,
    )


if __name__ == "__main__":
    main()
