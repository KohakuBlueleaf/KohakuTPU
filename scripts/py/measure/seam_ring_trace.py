"""Lead-only XDMA flight recorder around an existing card script; no extra polls.

python seam_ring_trace.py --output DIR SCRIPT [SCRIPT_ARGS...]
Writes the last 64 transfers and slowest eight, then queue/package evidence on
collect failure before application teardown. Not a throughput benchmark.
"""

import argparse
import json
import runpy
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from kohakuaccel.driver.node.queue import NodeError
from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.application.runtime.card import NodeCard


class Trace:
    def __init__(self, output):
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.recent = deque(maxlen=64)
        self.slowest = []
        self.bases = {}
        self.queues = {}
        self.retry = XdmaLinux._retry
        self.collect = NodeCard.collect
        self.started = time.monotonic()
        self.count = 0

    def transfer(self, transport, kind, at, n, xfer):
        start = time.monotonic()
        result = self.retry(transport, kind, at, n, xfer)
        end = time.monotonic()
        fields = {}
        for node, base in self.bases.items():
            layout = self.queues[node].layout
            for name in ("SQ_TAIL", "SQ_HEAD", "CQ_TAIL", "CQ_HEAD"):
                off = base + getattr(layout, name.lower()) - at
                if 0 <= off <= n - 8:
                    fields[f"n{node}:{name}"] = struct.unpack_from(
                        "<Q", transport._staging, off
                    )[0]
        event = {
            "kind": kind,
            "at": hex(at),
            "bytes": n,
            "start": start - self.started,
            "end": end - self.started,
            "seconds": end - start,
            "thread": threading.get_ident(),
            "pointers": fields,
        }
        self.count += 1
        self.recent.append(event)
        self.slowest = sorted(
            [*self.slowest, event], key=lambda row: row["seconds"], reverse=True
        )[:8]
        return result

    def summary(self):
        return {
            "transfers": self.count,
            "recent": list(self.recent),
            "slowest": self.slowest,
        }

    def collecting(self, card, pending, *args, **kw):
        self.bases = {n: card.mem._map(q.base) for n, q in card.queues.items()}
        self.queues = dict(card.queues)
        try:
            return self.collect(card, pending, *args, **kw)
        except (TimeoutError, NodeError) as exc:
            report = {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "completion": (
                    asdict(exc.completion) if isinstance(exc, NodeError) else None
                ),
                "trace": self.summary(),
                "firmware": str(card.fw),
                "transport_counts": dict(card.t.counts),
                "transport_faults": list(card.t.faults),
                "nodes": {},
            }
            path = self.output / "failure.json"
            path.write_text(json.dumps(report, indent=2) + "\n")
            for node, (tag, pkg) in pending.tags.items():
                name = f"n{node}-tag{tag:x}.pkg"
                (self.output / name).write_bytes(pkg.to_bytes())
                q = card.queues[node]
                layout = q.layout
                pointers = q.pointers()
                cq_at = q.base + q.geo["cq"] + q.cq_head % q.cq_n * layout.cq_bytes
                cq = struct.unpack("<4Q", q.mem.read_block(cq_at, layout.cq_bytes))
                sq_at = (
                    q.base
                    + q.geo["sq"]
                    + pointers["sq_head"] % q.sq_n * layout.sq_bytes
                )
                sq = struct.unpack(
                    f"<{layout.sq_words}Q", q.mem.read_block(sq_at, layout.sq_bytes)
                )
                report["nodes"][node] = {
                    "tag": tag,
                    "package": name,
                    "bindings": list(pending.plain[node][1] or ()),
                    "pointers": pointers,
                    "sq_entry": sq,
                    "cq_entry": cq,
                    "cq_head_slot_status": cq[1] & 0xFFFF,
                    "stdout_already_read": q.console,
                }
                path.write_text(json.dumps(report, indent=2) + "\n")
            print(f"SEAM failure evidence: {path}", file=sys.stderr, flush=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("script")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    trace = Trace(args.output)
    sys.argv = [args.script, *args.args]
    with (
        patch.object(XdmaLinux, "_retry", lambda t, *a: trace.transfer(t, *a)),
        patch.object(
            NodeCard, "collect", lambda c, *a, **kw: trace.collecting(c, *a, **kw)
        ),
    ):
        runpy.run_path(args.script, run_name="__main__")
    (args.output / "completed.json").write_text(
        json.dumps(trace.summary(), indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
