"""Same file transport, native write opcode versus Python dev.write dispatch."""

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from unittest.mock import patch

from hoste_direct import FileQueue
from kohakutpu.application.runtime import device as D
from kohakutpu.compiler import _rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("KTPU_NATIVE_MEMORY", "1")
    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    backend = FileQueue(dev)
    address = dev.alloc(256)
    _, physical = dev.mmu.resolve(address)
    raw = bytes(range(256))
    program = _rs.LaunchProgram(
        [("write", ("literal", address, None, 0), raw)],
        [],
        0,
        (dev.recipe_epoch, dev.recipe_epoch.generation),
    )

    def callback(at, data):
        _, where = dev.mmu.resolve(at, len(data))
        backend.endpoint.write_host(dev.card, where, data)
        dev.counters["bytes_in"] += len(data)

    samples = {False: [], True: []}
    try:
        with patch.object(dev, "write", callback):
            for turn in range(12):
                for mode in ((False, True) if turn % 2 else (True, False)):
                    dev.card.native_direct = backend.endpoint if mode else None
                    start = time.process_time_ns()
                    for _ in range(500):
                        program.launch(dev, [])
                    samples[mode].append((time.process_time_ns() - start) / 1e6 / 500)
        result = {
            str(mode): {"median_ms": statistics.median(values[2:]), "samples": values}
            for mode, values in samples.items()
        }
        assert backend.file.seek(physical) == physical
        assert backend.file.read(len(raw)) == raw
    finally:
        backend.endpoint.close()
        backend.file.close()
        dev.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
