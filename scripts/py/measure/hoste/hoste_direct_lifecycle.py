"""Bounded file-backed timeout witness, not a model or physical-card run.

A completion after60seconds must succeed under a65second budget. The old clamp
is reproduced by configuring60seconds for the identical delayed completion.
"""

import argparse
import json
import os
import threading
import time
from pathlib import Path

from hoste_direct import FileQueue
from kohakutpu.application.runtime import device as D


def measure(seconds):
    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    backend = FileQueue(dev)
    layout = backend.layout
    backend.queue.timeout = seconds
    dev.card.native_direct = backend.endpoint

    def firmware():
        deadline = time.monotonic() + 5
        while int.from_bytes(os.pread(backend.fd, 8, layout.sq_tail), "little") == 0:
            if time.monotonic() > deadline:
                return
            time.sleep(0.001)
        time.sleep(60.2)
        # Tag is already allocated; ready() normally prepares the next tag.
        tag = backend.queue._tag
        head = backend.queue.cq_head
        os.pwrite(
            backend.fd,
            layout.line_bytes(tag, 0, 123, 0)[: layout.cq_bytes],
            backend.geo["cq"] + head % 4 * layout.cq_bytes,
        )
        os.pwrite(
            backend.fd,
            layout.line_bytes(tag) + bytes(64),
            backend.geo["rec"] + head % 4 * layout.rec_bytes,
        )
        os.pwrite(backend.fd, layout.line_bytes(head + 1), layout.cq_tail)

    worker = threading.Thread(target=firmware)
    worker.start()
    start = time.monotonic()
    try:
        try:
            result = backend.endpoint.submit(dev.card, bytes([1]) * 64)
            outcome = {"success": True, "cycles": result["cycles"]}
        except OSError as error:
            outcome = {"success": False, "error": str(error)}
        outcome.update(timeout=seconds, elapsed=time.monotonic() - start)
    finally:
        worker.join()
        dev.close()
        backend.file.close()
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [measure(65.0), measure(60.0)]
    assert results[0]["success"] and not results[1]["success"], results
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
