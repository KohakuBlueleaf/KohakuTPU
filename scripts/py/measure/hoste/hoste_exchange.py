"""Callback-free endpoint versus queue Python, ready CQ in a regular file.

This isolates host publication/collection CPU. No DMA latency or tensor arithmetic
is modeled; the synthetic firmware completion is installed outside timing.
"""

import argparse
import json
import os
import statistics
import struct
import tempfile
import time
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakutpu.application.runtime.native_exchange import NativeExchange

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)


class Memory:
    def __init__(self, fd):
        self.fd = fd

    def _map(self, address, nbytes):
        return address

    def write_block(self, address, data):
        assert os.pwrite(self.fd, data, address) == len(data)

    def read_block(self, address, nbytes):
        return os.pread(self.fd, nbytes, address)

    def write64(self, address, value):
        self.write_block(address, struct.pack("<Q", value))

    def read64(self, address):
        return struct.unpack("<Q", self.read_block(address, 8))[0]


def measure(native, repeat):
    with tempfile.TemporaryFile() as file:
        file.truncate(1 << 20)
        memory = Memory(file.fileno())
        allocated = []

        def allocate(n):
            address = 65536 + sum(allocated)
            allocated.append(n)
            return address

        queue = NodeQueue(
            memory,
            0,
            sq_n=8,
            cq_n=8,
            spill=allocate,
            unspill=lambda _: None,
            record=LAYOUT.record,
            layout=LAYOUT,
        )
        queue.init()
        memory.write64(LAYOUT.fw, LAYOUT.st_ready)
        memory.write64(LAYOUT.list_abi, LAYOUT.list_version)
        upload = queue.prepare(bytes(64))
        execution = queue.prepare_list([(upload, (), 0, 0, i) for i in range(16)])
        queue.native_exchange = NativeExchange(
            queue,
            SimpleNamespace(
                _c2h=file.fileno(),
                _h2c=file.fileno(),
                fd_generation=1,
                _native_lock=RLock(),
            ),
        )
        samples = []
        for _ in range(repeat):
            tag = queue._tag + 1
            slot = queue.cq_head % queue.cq_n
            record = bytearray(LAYOUT.rec_bytes)
            struct.pack_into("<Q", record, LAYOUT.record.fields["tag"][0], tag)
            struct.pack_into(
                "<QQQ",
                record,
                LAYOUT.record.fields["list_index"][0],
                16,
                15,
                queue._generation,
            )
            memory.write_block(queue.geo["rec"] + slot * LAYOUT.rec_bytes, record)
            memory.write_block(
                queue.geo["cq"] + slot * LAYOUT.cq_bytes,
                struct.pack("<4Q", tag, 0, 100, 0),
            )
            memory.write64(LAYOUT.sq_head, queue.sq_tail)
            memory.write64(LAYOUT.cq_tail, queue.cq_head + 1)
            started = time.thread_time_ns()
            result = queue.wait(queue.submit_list(execution, native=native))
            samples.append((time.thread_time_ns() - started) / 1e6)
            assert result.cycles == 100 and result.record["list_index"] == 16
        execution.close()
        upload.close()
        return {
            "median_ms_per_list": statistics.median(samples),
            "median_ms_per_package": statistics.median(samples) / 16,
            "samples": samples,
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeat", type=int, default=1000)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = {str(native): measure(native, args.repeat) for native in (False, True)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: {a: b for a, b in v.items() if a != "samples"}
                for k, v in result.items()
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
